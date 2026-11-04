"""InlineDispatcher: run the job in the caller. Tests and debugging only."""

from __future__ import annotations

from collections.abc import Callable

from patient_access.core.models import JobId


class InlineDispatcher:
    def __init__(self, run: Callable[[JobId], object]) -> None:
        self.run = run

    def dispatch(self, job_id: JobId) -> None:
        self.run(job_id)
