"""In-memory plugs: the reference behaviour of every Protocol, for tests and quick experiments."""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable, Iterable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Generic
from uuid import uuid4

from patient_access.core.interfaces import DeliveryProgress
from patient_access.core.models import (
    TERMINAL_STATUSES,
    A,
    AuthAttempt,
    BlobMeta,
    Endpoint,
    Job,
    JobId,
    JobStatus,
    JsonObject,
    PendingUpload,
    S3Receipt,
    Selection,
    SpoolIndex,
)
from patient_access.core.settings import S3Settings
from patient_access.core.source_settings import SourceSettings
from patient_access.destinations.layout import TemplateLayout
from patient_access.formatters.stream import Digest


def utcnow() -> datetime:
    return datetime.now(UTC)


class MemoryDirectory:
    def __init__(self, selections: Iterable[Selection], settings: Callable[[Endpoint], SourceSettings]) -> None:
        self.selections = {s.id: s for s in selections}
        self._settings = settings

    def selection(self, selection_id: str) -> Selection:
        return self.selections[selection_id]

    def settings_for(self, endpoint: Endpoint) -> SourceSettings:
        return self._settings(endpoint)


class MemoryAttemptStore(Generic[A]):
    def __init__(self, clock: Callable[[], datetime] = utcnow) -> None:
        self.clock = clock
        self.attempts: dict[str, AuthAttempt[A]] = {}
        self.used: set[str] = set()

    def create(self, attempt: AuthAttempt[A]) -> None:
        self.attempts[attempt.state] = attempt

    def consume(self, state: str) -> AuthAttempt[A]:
        attempt = self.attempts.get(state)
        if attempt is None or state in self.used or attempt.expires_at <= self.clock():
            raise KeyError(state)
        self.used.add(state)
        return attempt

    def sweep(self, older_than: timedelta) -> int:
        cutoff = self.clock() - older_than
        gone = [s for s, a in self.attempts.items() if a.expires_at < cutoff]
        for s in gone:
            del self.attempts[s]
        return len(gone)


class MemoryJobStore(Generic[A]):
    """Stores jobs by value (a deep copy in, a deep copy out), like a database would."""

    def __init__(self, clock: Callable[[], datetime] = utcnow) -> None:
        self.clock = clock
        self._jobs: dict[JobId, Job[A]] = {}
        self._leases: dict[JobId, tuple[str, datetime]] = {}
        self._lock = threading.Lock()
        self.saves = 0

    def _copy(self, job: Job[A]) -> Job[A]:
        copy = job.model_copy(deep=True)
        copy.credentials = job.credentials.model_copy(deep=True)
        return copy

    def create(self, job: Job[A]) -> Job[A]:
        job = self._copy(job)
        job.id = job.id or uuid4().hex
        job.updated_at = self.clock()
        self._jobs[job.id] = job
        return self._copy(job)

    def get(self, job_id: JobId) -> Job[A]:
        return self._copy(self._jobs[job_id])

    def save(self, job: Job[A]) -> None:
        self.saves += 1
        job.updated_at = self.clock()
        self._jobs[job.id] = self._copy(job)

    def _leased_elsewhere(self, job_id: JobId, worker: str) -> bool:
        lease = self._leases.get(job_id)
        return lease is not None and lease[0] != worker and lease[1] > self.clock()

    def claim(self, job_id: JobId, worker: str, lease: timedelta) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status in TERMINAL_STATUSES or self._leased_elsewhere(job_id, worker):
                return False
            self._leases[job_id] = (worker, self.clock() + lease)
            return True

    def claim_next(self, worker: str, lease: timedelta, retry_after: timedelta) -> JobId | None:
        with self._lock:
            now = self.clock()
            for job in sorted(self._jobs.values(), key=lambda j: j.created_at):
                if job.status in TERMINAL_STATUSES or self._leased_elsewhere(job.id, worker):
                    continue
                if job.status is JobStatus.FAILED and (job.updated_at or now) > now - retry_after:
                    continue
                self._leases[job.id] = (worker, now + lease)
                return job.id
            return None

    def renew(self, job_id: JobId, worker: str, lease: timedelta) -> bool:
        with self._lock:
            held = self._leases.get(job_id)
            if held is None or held[0] != worker:
                return False
            self._leases[job_id] = (worker, self.clock() + lease)
            return True

    def release(self, job_id: JobId, worker: str) -> None:
        with self._lock:
            held = self._leases.get(job_id)
            if held is not None and held[0] == worker:
                del self._leases[job_id]

    def live_spool_refs(self) -> frozenset[str]:
        return frozenset(j.spool_ref for j in self._jobs.values() if j.spool_ref and not j.status.terminal)


class MemoryBlobWriter:
    def __init__(self, handle: MemorySpoolHandle, blob_id: str) -> None:
        self.handle, self.blob_id = handle, blob_id
        self._buf = bytearray()
        self.done = False

    @property
    def size(self) -> int:
        return len(self._buf)

    def write(self, chunk: bytes) -> None:
        self._buf += chunk

    def commit(self, *, content_type: str, referenced_by: str, source_url: str | None) -> BlobMeta:
        meta = BlobMeta(
            content_type=content_type,
            size=len(self._buf),
            sha256=hashlib.sha256(self._buf).hexdigest(),
            referenced_by=referenced_by,
            source_url=source_url,
        )
        self.handle.blobs[self.blob_id] = (bytes(self._buf), meta)
        self.done = True
        return meta

    def abort(self) -> None:
        self.done = True


class MemorySpoolHandle:
    def __init__(self, ref: str) -> None:
        self.ref = ref
        self.types: dict[str, list[JsonObject]] = {}
        self.blobs: dict[str, tuple[bytes, BlobMeta]] = {}

    def append(self, resource_type: str, resource: JsonObject) -> int:
        self.types.setdefault(resource_type, []).append(resource)
        return len(str(resource))

    def flush(self, resource_type: str) -> None:
        pass

    def reset_type(self, resource_type: str) -> None:
        self.types.pop(resource_type, None)

    def open_blob(self, blob_id: str) -> MemoryBlobWriter:
        return MemoryBlobWriter(self, blob_id)

    def has_blob(self, blob_id: str) -> bool:
        return blob_id in self.blobs

    def iter_type(self, resource_type: str) -> Iterator[JsonObject]:
        yield from (dict(r) for r in self.types.get(resource_type, []))

    def iter_blob(self, blob_id: str) -> Iterator[bytes]:
        data = self.blobs[blob_id][0]
        for i in range(0, len(data), 7):  # odd-sized chunks, to exercise the base64 carry
            yield data[i : i + 7]

    def index(self) -> SpoolIndex:
        return SpoolIndex(
            types={t: len(rs) for t, rs in self.types.items()},
            blobs={k: meta for k, (_, meta) in self.blobs.items()},
        )

    def close(self) -> None:
        pass


class MemorySpool:
    def __init__(self) -> None:
        self.handles: dict[str, MemorySpoolHandle] = {}

    def open(self, ref: str, key: bytes) -> MemorySpoolHandle:
        return self.handles.setdefault(ref, MemorySpoolHandle(ref))

    def delete(self, ref: str) -> None:
        self.handles.pop(ref, None)

    def sweep(self, keep: frozenset[str], older_than: timedelta) -> list[str]:
        gone = [r for r in self.handles if r not in keep]
        for r in gone:
            del self.handles[r]
        return gone


class MemorySink(Generic[A]):
    """Keeps every delivered object's bytes, by key."""

    def __init__(self, fail_times: int = 0) -> None:
        self.objects: dict[str, bytes] = {}
        self.fail_times = fail_times
        self.aborted: list[PendingUpload] = []

    def put(self, job: Job[A], chunks: Iterable[bytes], settings: S3Settings, progress: DeliveryProgress) -> S3Receipt:
        key = TemplateLayout(settings.layout).render(job)
        progress.started(PendingUpload(bucket=settings.bucket, key=key, upload_id=f"upload-{len(self.objects)}"))
        digest = Digest(chunks)
        data = b"".join(digest)
        if self.fail_times:
            self.fail_times -= 1
            raise ConnectionError("simulated outage")
        self.objects[key] = data
        progress.ended()
        return S3Receipt(bucket=settings.bucket, key=key, etag="etag", bytes=digest.size, sha256=digest.sha256, parts=1)

    def abort(self, upload: PendingUpload, settings: S3Settings) -> None:
        self.aborted.append(upload)
