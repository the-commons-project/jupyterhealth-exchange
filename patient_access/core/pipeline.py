"""run_job: the worker side of a pull, resumable by phase.

    QUEUED / FETCHING   fetch every granted type and its documents into the spool
    FETCHED / FAILED    deliver: the host's guard, then the bundle rendered and stored
    DONE / FAILED_TERMINAL

The job is saved after every step, every save fenced by the worker's lease. A retry after a
delivery failure starts at `fetched` and never touches the EHR again; a resumed run whose spool
was lost (another machine, a redeploy) refetches while the token lives and fails honestly when it
does not. A pull is all or nothing: when the EHR's authorization ends before every granted type
and document was fetched, the job fails and asks the patient to connect again, rather than
delivering part of the record. Scheduling and claiming are not its concern: the runner calls it
under a lease, and `LeaseLost` from any save stops it without another write.
"""

from __future__ import annotations

import base64
import secrets
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Generic

import requests
from pydantic import SecretStr, ValidationError

from .client import FhirClient
from .documents.run import fetch_documents
from .interfaces import Directory, JobStore, ObjectSink, Spool, SpoolHandle
from .lease import Lease, LeaseLost
from .log import JobLog, Phase, job_log, log_failure, sanitised
from .models import (
    A,
    Delivery,
    DeliveryStatus,
    Job,
    JobStatus,
    OutcomeStatus,
    PendingUpload,
)
from .puller import FetchAborted, fetch_resources
from .settings import BundleSettings, PatientAccessSettings
from .vocab import FailureKind

FHIR_JSON = "application/fhir+json"

AUTHORIZATION_ENDED = "The authorization ended before all your records were downloaded. Please connect again."


def utcnow() -> datetime:
    return datetime.now(UTC)


class DeliveryRefused(Exception):
    """The host's delivery guard refuses this delivery (e.g. consent withdrawn). Terminal. The
    message is safe to show the patient."""


SettingsProvider = Callable[[], PatientAccessSettings]
DeliveryGuard = Callable[[Job[A]], A]
"""Called just before every delivery attempt. Returns the participant's attributes as they stand
now (the host may refresh them, e.g. the studies the patient still consents to), which the bundle
then records; raises DeliveryRefused to stop the delivery for good."""
Renderer = Callable[[Job[A], SpoolHandle, BundleSettings, datetime], Iterable[bytes]]
"""The spool as the delivered object's bytes (the document Bundle: `formatters.render_bundle`)."""


@dataclass(frozen=True)
class Deps(Generic[A]):
    """The worker's collaborators. A frozen dataclass, not a pydantic model: it holds Protocols
    and callables, there is nothing to validate or serialise."""

    jobs: JobStore[A]
    spool: Spool
    directory: Directory
    sink: ObjectSink[A]
    settings_provider: SettingsProvider
    delivery_guard: DeliveryGuard[A]
    render: Renderer[A]
    clock: Callable[[], datetime] = utcnow
    http: requests.Session | None = None  # None: one per fetch, with the profile's retries
    max_attempts: int = 3


def run_job(lease: Lease, deps: Deps[A]) -> Job[A]:
    job = deps.jobs.get(lease.job_id)
    if job.status.terminal:
        return job
    job.runs += 1
    log = job_log(job, Phase.RUN)
    try:
        settings = deps.settings_provider()
    except (ValidationError, ValueError, KeyError) as e:
        # Nothing is lost: the next run reads the settings again.
        job.failure, job.failure_kind = f"settings not usable: {type(e).__name__}", FailureKind.RETRY_LATER
        deps.jobs.save(job, lease)
        log.error("settings not usable (%s); will retry", type(e).__name__)
        return job
    if job.credentials.spool_key is None:
        job.credentials.spool_key = SecretStr(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())
    handle = deps.spool.open(
        job.spool_ref or job.id, base64.urlsafe_b64decode(job.credentials.spool_key.get_secret_value())
    )
    job.spool_ref = handle.ref
    deps.jobs.save(job, lease)
    try:
        if job.runs > 1 and not _spool_intact(job, handle, deps, lease, log):
            return job
        if job.status in (JobStatus.QUEUED, JobStatus.FETCHING):
            _fetch(job, handle, settings, deps, lease)
        lease.check()
        if job.status in (JobStatus.FETCHED, JobStatus.DELIVERING, JobStatus.FAILED):
            _deliver(job, handle, settings, deps, lease)
    finally:
        handle.close()
    return job


def _spool_intact(job: Job[A], handle: SpoolHandle, deps: Deps[A], lease: Lease, log: JobLog) -> bool:
    """On resume, never trust the row's "ok" marks over the spool: a machine that died may have
    taken a local-disk spool with it. Lost types are refetched while the token lives; once it
    has expired the job fails rather than delivering a silently incomplete pull."""
    expected: dict[str, int] = {}
    for o in job.outcomes:
        if o.status is OutcomeStatus.OK:
            expected[o.resource_type] = expected.get(o.resource_type, 0) + o.count
    held = handle.index()
    lost = sorted(t for t, n in expected.items() if n and held.types.get(t, 0) != n)
    lost_blobs = frozenset(r.blob_id for r in job.documents.stored if r.blob_id not in held.blobs)
    if not lost and not lost_blobs:
        return True
    if job.token_live(deps.clock()):
        log.warning("spool lost for %d types and %d documents; refetching", len(lost), len(lost_blobs))
        job.status = JobStatus.FETCHING
        for o in job.outcomes:
            if o.resource_type in lost:
                o.reset()
        for t in lost:
            handle.reset_type(t)
        if lost_blobs:
            job.documents.forget(lost_blobs)
            job.documents.status = OutcomeStatus.PENDING
        deps.jobs.save(job, lease)
        return True
    _fail_terminal(
        job,
        deps,
        lease,
        "The downloaded records were lost before they could be delivered and the authorization has expired. "
        "Please connect again.",
        FailureKind.RECONNECT,
    )
    return False


def _fetch(job: Job[A], handle: SpoolHandle, settings: PatientAccessSettings, deps: Deps[A], lease: Lease) -> None:
    log = job_log(job, Phase.FETCH)
    token = job.credentials.ehr_access_token
    if token is None or not job.token_live(deps.clock()):
        _fail_terminal(job, deps, lease, AUTHORIZATION_ENDED, FailureKind.RECONNECT)
        return
    try:
        source_settings = deps.directory.settings_for(job.source.endpoint)
    except KeyError:
        _fail_terminal(
            job,
            deps,
            lease,
            f"{job.source.brand_name} is not available for connection any more.",
            FailureKind.CONTACT_STUDY,
        )
        return
    job.status = JobStatus.FETCHING
    deps.jobs.save(job, lease)
    client = FhirClient(
        job.source.iss, token, source_settings.fetch, job.token_expires_at, deps.clock, deps.http, lease
    )
    save = lambda: deps.jobs.save(job, lease)  # noqa: E731 - a closure over the job, passed down
    try:
        complete = fetch_resources(job, handle, source_settings, client, save, log)
        if complete and job.documents.status is not OutcomeStatus.OK:
            complete = fetch_documents(
                job, handle, source_settings.fetch.documents, settings.documents, client, save, log
            )
        elif not complete:
            job.documents.status = OutcomeStatus.ERROR
            job.documents.error = "authorization ended before documents were fetched"
    except LeaseLost:
        raise
    except FetchAborted as e:
        _fail_terminal(job, deps, lease, str(e), FailureKind.RECONNECT)
        return
    except Exception as e:  # noqa: BLE001 - a bug or an outage; do not retry the EHR
        log_failure(log, "fetch failed", e)
        _fail_terminal(job, deps, lease, "The download failed. Please connect again.", FailureKind.RECONNECT)
        return
    if not complete:
        # All or nothing: part of a record delivered as if it were the whole would mislead.
        _fail_terminal(job, deps, lease, AUTHORIZATION_ENDED, FailureKind.RECONNECT)
        return
    # The token stays (encrypted) until the job finishes: if the spool is lost before delivery,
    # a live token means a refetch instead of asking the patient to sign in again.
    job.status = JobStatus.FETCHED
    deps.jobs.save(job, lease)
    log.info("fetched %d resources in %d requests", sum(o.count for o in job.outcomes), client.requests)


class _Progress:
    """The sink's report of an upload in flight, recorded on the job so a dead process's
    upload is aborted by the next run."""

    def __init__(self, job: Job[A], deps: Deps[A], lease: Lease) -> None:
        self.job, self.deps, self.lease = job, deps, lease

    def started(self, upload: PendingUpload) -> None:
        self.job.delivery.pending_uploads.append(upload)
        self.deps.jobs.save(self.job, self.lease)

    def ended(self, upload: PendingUpload) -> None:
        self.job.delivery.pending_uploads = [u for u in self.job.delivery.pending_uploads if u != upload]
        self.deps.jobs.save(self.job, self.lease)


def _abort_pending(delivery: Delivery, settings: PatientAccessSettings, deps: Deps[A], log: JobLog) -> None:
    """Abort every upload an earlier attempt left open; the ones that cannot be aborted now stay
    recorded for the next attempt (the bucket's lifecycle rule is the backstop)."""
    left: list[PendingUpload] = []
    for upload in delivery.pending_uploads:
        try:
            deps.sink.abort(upload, settings.s3)
        except Exception as e:  # noqa: BLE001 - best effort; kept for the next attempt
            log.warning("could not abort an earlier upload: %s", type(e).__name__)
            left.append(upload)
    delivery.pending_uploads = left


def _deliver(job: Job[A], handle: SpoolHandle, settings: PatientAccessSettings, deps: Deps[A], lease: Lease) -> None:
    log = job_log(job, Phase.DELIVER)
    delivery = job.delivery
    _abort_pending(delivery, settings, deps, log)
    job.status, job.failure, job.failure_kind = JobStatus.DELIVERING, None, None
    delivery.status, delivery.failure = DeliveryStatus.DELIVERING, None
    delivery.attempts += 1
    delivery.started_at = delivery.started_at or deps.clock()
    deps.jobs.save(job, lease)
    try:
        attrs = deps.delivery_guard(job)
    except DeliveryRefused as e:
        delivery.status, delivery.finished_at = DeliveryStatus.FAILED_TERMINAL, deps.clock()
        delivery.failure = "refused by the host's delivery guard"
        _fail_terminal(job, deps, lease, str(e), FailureKind.CONSENT_WITHDRAWN)
        return
    except LeaseLost:
        raise
    except Exception as e:  # noqa: BLE001 - a guard that cannot decide counts as a failed attempt
        _delivery_failed(job, deps, lease, log, e)
        return
    job.participant = job.participant.model_copy(update={"attrs": attrs})
    try:
        chunks = deps.render(job, handle, settings.bundle, deps.clock())
        receipt = deps.sink.put(job, chunks, settings.s3, _Progress(job, deps, lease))
    except LeaseLost:
        raise
    except Exception as e:  # noqa: BLE001 - the spool still has the data; retry later
        _delivery_failed(job, deps, lease, log, e)
        return
    delivery.status, delivery.receipt, delivery.finished_at = DeliveryStatus.DONE, receipt, deps.clock()
    job.status = JobStatus.DONE
    log.info("delivered %d bytes in %d parts", receipt.bytes, receipt.parts)
    _finish(job, deps, lease)


def _delivery_failed(job: Job[A], deps: Deps[A], lease: Lease, log: JobLog, e: Exception) -> None:
    delivery = job.delivery
    log.error("delivery attempt %d failed: %s %s", delivery.attempts, type(e).__name__, sanitised(str(e)))
    if delivery.attempts >= deps.max_attempts:
        delivery.status, delivery.finished_at = DeliveryStatus.FAILED_TERMINAL, deps.clock()
        delivery.failure = f"delivery failed after {delivery.attempts} attempts ({type(e).__name__})"
        _fail_terminal(
            job,
            deps,
            lease,
            "We could not deliver your records. Please contact your study team.",
            FailureKind.CONTACT_STUDY,
        )
    else:
        delivery.status = DeliveryStatus.FAILED
        delivery.failure = f"delivery failed, will retry ({type(e).__name__})"
        job.status, job.failure, job.failure_kind = JobStatus.FAILED, delivery.failure, FailureKind.RETRY_LATER
        deps.jobs.save(job, lease)


def _fail_terminal(job: Job[A], deps: Deps[A], lease: Lease, reason: str, kind: FailureKind) -> None:
    job.status, job.failure, job.failure_kind = JobStatus.FAILED_TERMINAL, reason, kind
    job_log(job, Phase.END).warning("failed (%s): %s", kind.value, sanitised(reason, 120))
    _finish(job, deps, lease)


def _finish(job: Job[A], deps: Deps[A], lease: Lease) -> None:
    """Terminal: the credentials cleared, which crypto-shreds anything the spool left behind, and
    the spool deleted. The fenced save comes first: a worker that lost the lease must not delete
    the spool the new owner is using. A spool whose delete fails is unreferenced from then on,
    and the sweep removes it."""
    spool_ref, job.spool_ref = job.spool_ref, None
    job.credentials.clear()
    job.token_expires_at = None
    job.finished_at = deps.clock()
    deps.jobs.save(job, lease)
    if spool_ref:
        deps.spool.delete(spool_ref)
