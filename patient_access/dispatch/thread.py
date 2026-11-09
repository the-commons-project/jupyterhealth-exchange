"""ThreadDispatcher: run the job in a daemon thread of the web process, right after dispatch.

The no-new-infrastructure runner for a host like JHE (sync gunicorn, no broker): the callback
redirects at once and the pull proceeds in the background of the same process. The runner claims
the job under a lease, so the cron sweep never runs it at the same time, and picks up anything a
thread dropped once the lease lapses.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from patient_access.core.models import JobId


class ThreadDispatcher:
    def __init__(
        self,
        run: Callable[[JobId], bool],
        on_thread_exit: Callable[[], None] = lambda: None,
        thread_factory: Callable[..., threading.Thread] = threading.Thread,
    ) -> None:
        """`run`: the runner's claim-and-run. `on_thread_exit`: host cleanup for the thread."""
        self.run = run
        self.on_thread_exit = on_thread_exit
        self.thread_factory = thread_factory

    def dispatch(self, job_id: JobId) -> None:
        self.thread_factory(target=self._work, args=(job_id,), name=f"pa-job-{job_id[:8]}", daemon=True).start()

    def _work(self, job_id: JobId) -> None:
        try:
            self.run(job_id)
        finally:
            self.on_thread_exit()
