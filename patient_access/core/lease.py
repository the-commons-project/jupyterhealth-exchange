"""The lease a worker holds on one job while it runs it.

Workers are separate processes, often on separate machines (the web process's thread, the cron
sweep), so the only structure they share is the job row. A lease on the row says who may write
it. Two rules make that hold however a worker is delayed:

- **Fenced writes.** Every save names the lease; the store writes only while that worker still
  owns the row, and raises `LeaseLost` otherwise. A worker that stalled past its lease can never
  write a status over the one that took the job from it.
- **Prompt stopping.** When the heartbeat finds the lease gone, it cancels the lease; the worker
  checks before every request to the EHR and stops early, rather than discovering the loss only
  at its next save.

Delivery is idempotent besides: the object key is fixed per job, so a stale upload that did
complete is overwritten by an identical one.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from .models import JobId


class LeaseLost(Exception):
    """This worker no longer owns the job. Stop without writing anything more."""


@dataclass(frozen=True)
class Lease:
    """One worker's claim on one job. A frozen dataclass, not a pydantic model: it carries a
    thread primitive (the cancellation, set by the heartbeat thread, read by the worker)."""

    job_id: JobId
    worker: str
    _cancelled: threading.Event = field(default_factory=threading.Event, compare=False, repr=False)

    def cancel(self) -> None:
        self._cancelled.set()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def check(self) -> None:
        """Raises LeaseLost once the heartbeat has given the lease up."""
        if self._cancelled.is_set():
            raise LeaseLost(self.job_id)
