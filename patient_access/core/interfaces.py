"""The plugs. Core depends on these Protocols; implementations live one layer out.

A pull runs in five phases:

    1. open      the host authenticates the patient and builds a Participant   (host code)
    2. pick      the patient chooses where to connect                          Directory
    3. authorize SMART launch, callback, token exchange, job created           Directory, AuthAttemptStore,
                                                                              JobStore, JobDispatcher
    4. fetch     every granted type and its documents into the spool          Spool, SpoolHandle, BlobWriter
    5. deliver   the spool rendered as one document Bundle and stored         ObjectSink, DeliveryProgress

Phases 1-3 run in the web process (`flow.py`); 4-5 run in a worker (`pipeline.run_job`). The
in-memory implementations in `patient_access.testing.memory` are the reference behaviour.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from datetime import timedelta
from typing import Protocol

from .models import (
    A,
    AuthAttempt,
    BlobMeta,
    Endpoint,
    Job,
    JobId,
    JsonObject,
    PendingUpload,
    S3Receipt,
    Selection,
    SpoolIndex,
)
from .settings import S3Settings
from .source_settings import SourceSettings


class Directory(Protocol):
    """Who the patient can connect to, and how. JHE maps it over its EhrVendor, EhrBrand and
    EhrBrandLocation rows; it is the only code that reads those tables."""

    def selection(self, selection_id: str) -> Selection:
        """The selection a picker id refers to. Raises KeyError when unknown."""
        ...

    def settings_for(self, endpoint: Endpoint) -> SourceSettings:
        """Registration and fetch profile for an endpoint. Called at launch, at the callback and
        once per worker run, never stored, so a changed client id takes effect on the next call.
        Raises KeyError when the endpoint's vendor is not set up for connection."""
        ...


class AuthAttemptStore(Protocol[A]):
    """Phase 3's pending authorizations, keyed by the OAuth `state`. Implementations encrypt the
    PKCE verifier at rest."""

    def create(self, attempt: AuthAttempt[A]) -> None: ...

    def consume(self, state: str) -> AuthAttempt[A]:
        """The callback's single read. Marks the attempt used, so a replayed callback finds
        nothing. Raises KeyError when the state is unknown, expired or already used."""
        ...

    def sweep(self, older_than: timedelta) -> int:
        """Delete attempts that expired longer ago than `older_than`. Returns how many."""
        ...


class JobStore(Protocol[A]):
    """Where jobs live from the callback to the end of delivery. The pipeline saves after every
    step, so a worker that dies leaves an honest row to resume from. Claiming is here too: a
    lease on the row is what stops the thread and the cron sweep running the same job."""

    def create(self, job: Job[A]) -> Job[A]:
        """Persist a new job and return it with its id assigned."""
        ...

    def get(self, job_id: JobId) -> Job[A]:
        """Raises KeyError."""
        ...

    def save(self, job: Job[A]) -> None:
        """Write the whole record back, credentials included (encrypted)."""
        ...

    def claim(self, job_id: JobId, worker: str, lease: timedelta) -> bool:
        """Lease one named job, only if it is not terminal and not leased by someone else."""
        ...

    def claim_next(self, worker: str, lease: timedelta, retry_after: timedelta) -> JobId | None:
        """Lease the oldest runnable job: queued, abandoned mid-run (no lease, or an expired
        one), or failed and last touched longer than `retry_after` ago."""
        ...

    def renew(self, job_id: JobId, worker: str, lease: timedelta) -> bool:
        """Extend a lease this worker holds. False when it no longer holds it."""
        ...

    def release(self, job_id: JobId, worker: str) -> None: ...

    def live_spool_refs(self) -> frozenset[str]:
        """Spool refs of jobs that are not terminal: the spools the sweep must keep."""
        ...


class BlobWriter(Protocol):
    """One attachment being written into the spool, streamed. Nothing is visible until
    `commit`; `abort` (or never committing) leaves nothing behind."""

    def write(self, chunk: bytes) -> None: ...

    @property
    def size(self) -> int:
        """Plaintext bytes written so far."""
        ...

    def commit(self, *, content_type: str, referenced_by: str, source_url: str | None) -> BlobMeta:
        """Make the blob durable and visible; the spool computed size and SHA-256 as it went."""
        ...

    def abort(self) -> None: ...


class SpoolHandle(Protocol):
    """One job's spool, open. Resources are appended per type and read back in order; blobs are
    the documents the fetch downloaded. Everything is encrypted with the job's spool key."""

    ref: str

    def append(self, resource_type: str, resource: JsonObject) -> int:
        """Add one resource under its type. Returns the bytes written. Durable after `flush`."""
        ...

    def flush(self, resource_type: str) -> None:
        """Make the type durable: called before an outcome saying "ok" is saved."""
        ...

    def reset_type(self, resource_type: str) -> None:
        """Drop what an unfinished attempt wrote for this type, so it can be refetched."""
        ...

    def open_blob(self, blob_id: str) -> BlobWriter: ...

    def has_blob(self, blob_id: str) -> bool: ...

    def iter_type(self, resource_type: str) -> Iterator[JsonObject]: ...

    def iter_blob(self, blob_id: str) -> Iterator[bytes]:
        """A committed blob's plaintext, in chunks. Raises KeyError for an unknown id."""
        ...

    def index(self) -> SpoolIndex: ...

    def close(self) -> None: ...


class Spool(Protocol):
    def open(self, ref: str, key: bytes) -> SpoolHandle:
        """Create the spool `ref`, or reopen it. `key` is the job's 32-byte spool key."""
        ...

    def delete(self, ref: str) -> None:
        """Remove a spool. Tolerates one that is already gone."""
        ...

    def sweep(self, keep: frozenset[str], older_than: timedelta) -> list[str]:
        """Delete spools not in `keep` and older than `older_than`. Returns what was deleted."""
        ...


class DeliveryProgress(Protocol):
    """How a sink tells the pipeline about an upload in flight, so it is recorded on the job and
    aborted if the process dies before completing it. The sink never touches the job store."""

    def started(self, upload: PendingUpload) -> None: ...

    def ended(self) -> None: ...


class ObjectSink(Protocol[A]):
    """Stores the rendered bundle as one object."""

    def put(
        self, job: Job[A], chunks: Iterable[bytes], settings: S3Settings, progress: DeliveryProgress
    ) -> S3Receipt: ...

    def abort(self, upload: PendingUpload, settings: S3Settings) -> None:
        """Abort an upload a previous attempt left open. Tolerates one that is already gone."""
        ...


class JobDispatcher(Protocol):
    """Hands a new job to whatever runs the worker side. Must return quickly. A lost dispatch is
    recoverable: the row is the real queue and the cron sweep claims it."""

    def dispatch(self, job_id: JobId) -> None: ...
