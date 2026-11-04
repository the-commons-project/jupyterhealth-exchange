"""JobRunner: claim, lease, run, release, so the web thread and the cron sweep never run the same
job at once, and anything a dead process dropped is picked up again.

The lease is short and kept alive by a heartbeat while the job runs. A process that dies however
it dies (SIGKILL, OOM, a machine stop, the dev server's reloader child) stops renewing, the lease
expires within `lease`, and the next sweep resumes the job by phase. Graceful exits also release
at once (`release_all`, registered with atexit by the host).
"""

from __future__ import annotations

import logging
import os
import socket
import threading
from collections.abc import Callable
from datetime import timedelta
from typing import Generic

from .interfaces import AuthAttemptStore, JobStore, Spool
from .models import A, Job, JobId

log = logging.getLogger(__name__)

DEFAULT_LEASE = timedelta(seconds=60)
RETRY_AFTER = timedelta(seconds=30)  # a failed delivery waits this long before the sweep retries it
SPOOL_MAX_AGE = timedelta(hours=6)  # an unreferenced spool this old is deleted by the sweep
ATTEMPT_MAX_AGE = timedelta(days=1)  # expired auth attempts are deleted after this


def worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{threading.get_ident()}"


class _Heartbeat:
    """Renews one lease every `lease / 3` until stopped."""

    def __init__(
        self,
        jobs: JobStore[A],
        job_id: JobId,
        worker: str,
        lease: timedelta,
        on_thread_exit: Callable[[], None],
    ) -> None:
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._loop,
            args=(jobs, job_id, worker, lease, on_thread_exit),
            name=f"pa-lease-{job_id[:8]}",
            daemon=True,
        )

    def _loop(
        self, jobs: JobStore[A], job_id: JobId, worker: str, lease: timedelta, on_thread_exit: Callable[[], None]
    ) -> None:
        try:
            while not self._stop.wait(lease.total_seconds() / 3):
                if not jobs.renew(job_id, worker, lease):
                    log.warning("job %s: lease lost", job_id)
                    return
        except Exception:  # noqa: BLE001 - a lost heartbeat only means the sweep may resume the job
            log.exception("job %s: lease heartbeat failed", job_id)
        finally:
            on_thread_exit()

    def __enter__(self) -> _Heartbeat:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=5)


class JobRunner(Generic[A]):
    def __init__(
        self,
        jobs: JobStore[A],
        run_job: Callable[[JobId], Job[A]],
        lease: timedelta = DEFAULT_LEASE,
        on_thread_exit: Callable[[], None] = lambda: None,
        worker: Callable[[], str] = worker_id,
    ) -> None:
        """`on_thread_exit`: host cleanup for a thread the runner started (Django: close the
        thread's database connection)."""
        self.jobs = jobs
        self.run_job = run_job
        self.lease = lease
        self.on_thread_exit = on_thread_exit
        self.worker = worker
        self._active: dict[JobId, str] = {}
        self._lock = threading.Lock()

    def _run_leased(self, job_id: JobId, worker: str) -> None:
        with self._lock:
            self._active[job_id] = worker
        try:
            with _Heartbeat(self.jobs, job_id, worker, self.lease, self.on_thread_exit):
                self.run_job(job_id)
        except Exception:  # noqa: BLE001 - run_job records failures on the job; the sweep retries
            log.exception("job %s: run failed", job_id)
        finally:
            with self._lock:
                self._active.pop(job_id, None)
            self.jobs.release(job_id, worker)

    def run(self, job_id: JobId) -> bool:
        """Run one named job if it can be claimed. False when it is leased elsewhere or terminal."""
        worker = self.worker()
        if not self.jobs.claim(job_id, worker, self.lease):
            log.info("job %s: not claimed (leased elsewhere or terminal)", job_id)
            return False
        self._run_leased(job_id, worker)
        return True

    def run_next(self) -> JobId | None:
        worker = self.worker()
        job_id = self.jobs.claim_next(worker, self.lease, RETRY_AFTER)
        if job_id is not None:
            self._run_leased(job_id, worker)
        return job_id

    def drain(self) -> int:
        """Run every runnable job, one after another. Returns how many ran."""
        n = 0
        seen: set[JobId] = set()
        while (job_id := self.run_next()) is not None:
            n += 1
            if job_id in seen:
                break  # a job that stays runnable (e.g. settings unusable) runs once per sweep
            seen.add(job_id)
        return n

    def release_all(self) -> None:
        """Release every lease this process holds: for graceful shutdown."""
        with self._lock:
            active = dict(self._active)
            self._active.clear()
        for job_id, worker in active.items():
            try:
                self.jobs.release(job_id, worker)
            except Exception:  # noqa: BLE001 - best effort at shutdown
                log.exception("job %s: lease release at exit failed", job_id)


def sweep(
    jobs: JobStore[A],
    spool: Spool,
    attempts: AuthAttemptStore[A] | None = None,
    spool_max_age: timedelta = SPOOL_MAX_AGE,
    attempt_max_age: timedelta = ATTEMPT_MAX_AGE,
) -> tuple[list[str], int]:
    """Garbage collection: spools no live job refers to, and long-expired auth attempts."""
    gone = spool.sweep(jobs.live_spool_refs(), spool_max_age)
    swept = attempts.sweep(attempt_max_age) if attempts is not None else 0
    return gone, swept
