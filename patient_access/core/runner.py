"""JobRunner: claim, lease, run, release, so the web thread and the cron sweep never run the same
job at once, and anything a dead process dropped is picked up again.

The lease is short and kept alive by a heartbeat while the job runs. A process that dies however
it dies (SIGKILL, OOM, a machine stop, the dev server's reloader child) stops renewing, the lease
expires within `lease`, and the next sweep resumes the job by phase. Graceful exits also release
at once (`release_all`, registered with atexit by the host). Workers share nothing but the row,
so the lease is the only coordination; `lease.py` says how writes are fenced by it.

A run that raises out of the pipeline is a crash. Crashes are counted on the row, and a job that
crashes `MAX_CRASHES` times is ended, so one broken job can never block the others: a sweep also
visits each runnable job at most once.
"""

from __future__ import annotations

import os
import socket
import threading
import time
from collections.abc import Callable
from datetime import timedelta
from typing import Generic

from .interfaces import AuthAttemptStore, JobStore, Spool
from .lease import Lease, LeaseLost
from .log import lease_log, log_failure
from .models import A, Job, JobId

DEFAULT_LEASE = timedelta(seconds=60)
RETRY_AFTER = timedelta(seconds=30)  # a failed delivery waits this long before the sweep retries it
SPOOL_MAX_AGE = timedelta(hours=6)  # an unreferenced spool this old is deleted by the sweep
ATTEMPT_MAX_AGE = timedelta(days=1)  # expired auth attempts are deleted after this
MAX_CRASHES = 3  # runs that raised out of the pipeline before the job is ended


def worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{threading.get_ident()}"


LAPSE_MARGIN = 1 / 6  # of the lease: a worker gives up this much before the database would


class _Heartbeat:
    """Renews one lease every `period / 3` until stopped. A renewal that fails (the database
    unreachable) is retried, and the lease is cancelled a margin before it would really lapse
    (never after: a stale worker must be stopped before a new owner can start, since the two
    may share a spool directory), or as soon as the row turns out to belong to another worker."""

    def __init__(
        self,
        jobs: JobStore[A],
        lease: Lease,
        period: timedelta,
        on_thread_exit: Callable[[], None],
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._loop,
            args=(jobs, lease, period, on_thread_exit, monotonic),
            name=f"pa-lease-{lease.job_id[:8]}",
            daemon=True,
        )

    def _loop(
        self,
        jobs: JobStore[A],
        lease: Lease,
        period: timedelta,
        on_thread_exit: Callable[[], None],
        monotonic: Callable[[], float],
    ) -> None:
        log = lease_log(lease.job_id)
        seconds = period.total_seconds()
        give_up_after = seconds * (1 - LAPSE_MARGIN)
        renewed_at = monotonic()
        wait = seconds / 3
        try:
            while not self._stop.wait(wait):
                try:
                    held = jobs.renew(lease.job_id, lease.worker, period)
                except Exception as e:  # noqa: BLE001 - retried below until the margin
                    unrenewed = monotonic() - renewed_at
                    if unrenewed >= give_up_after:
                        log_failure(log, "lease could not be renewed in time; stopping", e)
                        lease.cancel()
                        return
                    log.warning("lease renewal failed (%s); retrying", type(e).__name__)
                    on_thread_exit()  # drop a broken connection, so the retry opens a fresh one
                    wait = min(seconds / 3, give_up_after - unrenewed)  # wake at the margin, not past it
                    continue
                wait = seconds / 3
                if not held:
                    log.warning("lease taken by another worker; stopping")
                    lease.cancel()
                    return
                renewed_at = monotonic()
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
        run_job: Callable[[Lease], Job[A]],
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

    def _run_leased(self, lease: Lease) -> None:
        log = lease_log(lease.job_id)
        with self._lock:
            self._active[lease.job_id] = lease.worker
        try:
            with _Heartbeat(self.jobs, lease, self.lease, self.on_thread_exit):
                self.run_job(lease)
        except LeaseLost:
            log.warning("lease lost while running; another worker has the job")
        except Exception as e:  # noqa: BLE001 - a crash: counted, and the job ended after MAX_CRASHES
            log_failure(log, "run failed", e)
            self._count_crash(lease)
        finally:
            with self._lock:
                self._active.pop(lease.job_id, None)
            self.jobs.release(lease.job_id, lease.worker)

    def _count_crash(self, lease: Lease) -> None:
        log = lease_log(lease.job_id)
        try:
            crashes = self.jobs.record_crash(lease)
            if crashes >= MAX_CRASHES:
                self.jobs.fail_crashed(lease, f"stopped after {crashes} runs that failed unexpectedly")
                log.error("ended after %d crashes", crashes)
        except LeaseLost:
            log.warning("lease lost before the crash was recorded")
        except Exception as e:  # noqa: BLE001 - the next sweep counts it again
            log_failure(log, "crash could not be recorded", e)

    def run(self, job_id: JobId) -> bool:
        """Run one named job if it can be claimed. False when it is leased elsewhere or terminal."""
        worker = self.worker()
        if not self.jobs.claim(job_id, worker, self.lease):
            lease_log(job_id).info("not claimed (leased elsewhere or terminal)")
            return False
        self._run_leased(Lease(job_id=job_id, worker=worker))
        return True

    def run_next(self, exclude: frozenset[JobId] = frozenset()) -> JobId | None:
        worker = self.worker()
        job_id = self.jobs.claim_next(worker, self.lease, RETRY_AFTER, exclude)
        if job_id is not None:
            self._run_leased(Lease(job_id=job_id, worker=worker))
        return job_id

    def drain(self) -> int:
        """Run every runnable job once, one after another. Returns how many ran. A job that stays
        runnable (it failed, or is waiting on something) is not visited again in this sweep, so
        it can never hold up the jobs behind it."""
        seen: frozenset[JobId] = frozenset()
        while (job_id := self.run_next(seen)) is not None:
            seen |= {job_id}
        return len(seen)

    def release_all(self) -> None:
        """Release every lease this process holds: for graceful shutdown."""
        with self._lock:
            active = dict(self._active)
            self._active.clear()
        for job_id, worker in active.items():
            try:
                self.jobs.release(job_id, worker)
            except Exception as e:  # noqa: BLE001 - best effort at shutdown
                log_failure(lease_log(job_id), "lease release at exit failed", e)


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
