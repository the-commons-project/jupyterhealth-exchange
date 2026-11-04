"""run_job: the worker side of a pull, resumable by phase.

    QUEUED / FETCHING   fetch every granted type and its documents into the spool
    FETCHED / FAILED    deliver: the host's guard, then the bundle rendered and stored
    DONE / FAILED_TERMINAL

The job is saved after every step. A retry after a delivery failure starts at `fetched` and
never touches the EHR again; a resumed run whose spool was lost (another machine, a redeploy)
refetches while the token lives and fails honestly when it does not. Scheduling and claiming
are not its concern: the runner calls it under a lease.
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
from .log import JobLog, job_log, sanitised
from .models import (
    A,
    Delivery,
    DeliveryStatus,
    Job,
    JobId,
    JobStatus,
    OutcomeStatus,
    PendingUpload,
)
from .puller import FetchAborted, fetch_resources
from .settings import BundleSettings, PatientAccessSettings

FHIR_JSON = "application/fhir+json"


def utcnow() -> datetime:
    return datetime.now(UTC)


class DeliveryRefused(Exception):
    """The host's delivery guard refuses this delivery (e.g. consent withdrawn). Terminal. The
    message is safe to show the patient."""


SettingsProvider = Callable[[], PatientAccessSettings]
DeliveryGuard = Callable[[Job[A]], None]
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


def run_job(job_id: JobId, deps: Deps[A]) -> Job[A]:
    job = deps.jobs.get(job_id)
    if job.status.terminal:
        return job
    job.runs += 1
    log = job_log(job, "run")
    try:
        settings = deps.settings_provider()
    except (ValidationError, ValueError, KeyError) as e:
        # Nothing is lost: the next run reads the settings again.
        job.failure = f"settings not usable: {type(e).__name__}"
        deps.jobs.save(job)
        log.error("settings not usable (%s); will retry", type(e).__name__)
        return job
    if job.credentials.spool_key is None:
        job.credentials.spool_key = SecretStr(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())
    handle = deps.spool.open(
        job.spool_ref or job.id, base64.urlsafe_b64decode(job.credentials.spool_key.get_secret_value())
    )
    job.spool_ref = handle.ref
    deps.jobs.save(job)
    try:
        if job.runs > 1 and not _spool_intact(job, handle, deps, log):
            return job
        if job.status in (JobStatus.QUEUED, JobStatus.FETCHING):
            _fetch(job, handle, settings, deps)
        if job.status in (JobStatus.FETCHED, JobStatus.DELIVERING, JobStatus.FAILED):
            _deliver(job, handle, settings, deps)
    finally:
        handle.close()
    return job


def _spool_intact(job: Job[A], handle: SpoolHandle, deps: Deps[A], log: JobLog) -> bool:
    """On resume, never trust the row's "ok" marks over the spool: a machine that died may have
    taken a local-disk spool with it. Lost types are refetched while the token lives; once it
    has expired the job fails rather than delivering a silently incomplete pull."""
    expected: dict[str, int] = {}
    for o in job.outcomes:
        if o.status is OutcomeStatus.OK:
            expected[o.resource_type] = expected.get(o.resource_type, 0) + o.count
    held = handle.index()
    lost = sorted(t for t, n in expected.items() if n and held.types.get(t, 0) != n)
    lost_blobs = [r.blob_id for r in job.documents.stored if r.blob_id not in held.blobs]
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
            job.documents.records = [r for r in job.documents.records if r.blob_id not in lost_blobs]
            job.documents.status = OutcomeStatus.PENDING
        deps.jobs.save(job)
        return True
    _fail_terminal(
        job,
        deps,
        "The downloaded records were lost before they could be delivered and the authorization has expired. "
        "Please connect again.",
    )
    return False


def _fetch(job: Job[A], handle: SpoolHandle, settings: PatientAccessSettings, deps: Deps[A]) -> None:
    log = job_log(job, "fetch")
    token = job.credentials.ehr_access_token
    if token is None or not job.token_live(deps.clock()):
        _fail_terminal(job, deps, "The authorization expired before the download finished. Please connect again.")
        return
    try:
        source_settings = deps.directory.settings_for(job.source.endpoint)
    except KeyError:
        _fail_terminal(job, deps, f"{job.source.brand_name} is not available for connection any more.")
        return
    job.status = JobStatus.FETCHING
    deps.jobs.save(job)
    client = FhirClient(job.source.iss, token, source_settings.fetch, job.token_expires_at, deps.clock, deps.http)
    save = lambda: deps.jobs.save(job)  # noqa: E731 - a closure over the job, passed down
    try:
        complete = fetch_resources(job, handle, source_settings, client, save, log)
        if complete and job.documents.status is not OutcomeStatus.OK:
            fetch_documents(job, handle, source_settings.fetch.documents, settings.documents, client, save, log)
    except FetchAborted as e:
        _fail_terminal(job, deps, str(e))
        return
    except Exception as e:  # noqa: BLE001 - a bug or an outage; do not retry the EHR
        log.exception("fetch failed: %s", type(e).__name__)
        _fail_terminal(job, deps, "The download failed. Please connect again.")
        return
    # The token stays (encrypted) until the job finishes: if the spool is lost before delivery,
    # a live token means a refetch instead of asking the patient to sign in again.
    job.status = JobStatus.FETCHED
    deps.jobs.save(job)
    log.info("fetched %d resources in %d requests", sum(o.count for o in job.outcomes), client.requests)


class _Progress:
    """The sink's report of an upload in flight, recorded on the job so a dead process's
    upload is aborted by the next run."""

    def __init__(self, job: Job[A], deps: Deps[A]) -> None:
        self.job, self.deps = job, deps

    def started(self, upload: PendingUpload) -> None:
        self.job.delivery.pending_upload = upload
        self.deps.jobs.save(self.job)

    def ended(self) -> None:
        self.job.delivery.pending_upload = None
        self.deps.jobs.save(self.job)


def _abort_pending(delivery: Delivery, settings: PatientAccessSettings, deps: Deps[A], log: JobLog) -> None:
    if delivery.pending_upload is None:
        return
    try:
        deps.sink.abort(delivery.pending_upload, settings.s3)
        delivery.pending_upload = None
    except Exception as e:  # noqa: BLE001 - best effort; the bucket's lifecycle rule is the backstop
        log.warning("could not abort an earlier upload: %s", type(e).__name__)


def _deliver(job: Job[A], handle: SpoolHandle, settings: PatientAccessSettings, deps: Deps[A]) -> None:
    log = job_log(job, "deliver")
    delivery = job.delivery
    _abort_pending(delivery, settings, deps, log)
    try:
        deps.delivery_guard(job)
    except DeliveryRefused as e:
        _fail_terminal(job, deps, str(e))
        return
    job.status, job.failure = JobStatus.DELIVERING, None
    delivery.status, delivery.failure = DeliveryStatus.DELIVERING, None
    delivery.attempts += 1
    delivery.started_at = delivery.started_at or deps.clock()
    deps.jobs.save(job)
    try:
        chunks = deps.render(job, handle, settings.bundle, deps.clock())
        receipt = deps.sink.put(job, chunks, settings.s3, _Progress(job, deps))
    except Exception as e:  # noqa: BLE001 - the spool still has the data; retry later
        detail = sanitised(str(e)) or type(e).__name__
        log.error("delivery attempt %d failed: %s %s", delivery.attempts, type(e).__name__, detail)
        if delivery.attempts >= deps.max_attempts:
            delivery.status, delivery.finished_at = DeliveryStatus.FAILED_TERMINAL, deps.clock()
            delivery.failure = f"delivery failed after {delivery.attempts} attempts ({type(e).__name__})"
            _fail_terminal(job, deps, "We could not deliver your records. Please contact your study team.")
        else:
            delivery.status = DeliveryStatus.FAILED
            delivery.failure = f"delivery failed, will retry ({type(e).__name__})"
            job.status, job.failure = JobStatus.FAILED, delivery.failure
            deps.jobs.save(job)
        return
    delivery.status, delivery.receipt, delivery.finished_at = DeliveryStatus.DONE, receipt, deps.clock()
    job.status = JobStatus.DONE
    log.info("delivered %d bytes in %d parts", receipt.bytes, receipt.parts)
    _finish(job, deps)


def _fail_terminal(job: Job[A], deps: Deps[A], reason: str) -> None:
    job.status, job.failure = JobStatus.FAILED_TERMINAL, reason
    job_log(job, "end").warning("failed: %s", sanitised(reason, 120))
    _finish(job, deps)


def _finish(job: Job[A], deps: Deps[A]) -> None:
    """Terminal: the spool is deleted and the credentials cleared, which crypto-shreds anything
    the spool left behind."""
    if job.spool_ref:
        deps.spool.delete(job.spool_ref)
        job.spool_ref = None
    job.credentials.clear()
    job.token_expires_at = None
    job.finished_at = deps.clock()
    deps.jobs.save(job)
