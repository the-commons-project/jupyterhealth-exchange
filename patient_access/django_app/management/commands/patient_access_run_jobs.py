"""One sweep, for cron: run every runnable job (queued, abandoned by a dead process, or a failed
delivery due a retry), then delete orphaned spools and long-expired sign-in attempts."""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand

from patient_access.core.runner import sweep

from ...wiring import NotConfigured, get_module


class Command(BaseCommand):
    help = "Run every runnable patient access job, then sweep orphaned spools (cron, every minute)."

    def handle(self, *args: Any, **options: Any) -> None:
        try:
            module = get_module()
        except NotConfigured:
            return  # the module is off in this deployment: nothing to do
        ran = module.runner.drain()
        spools, attempts = sweep(module.jobs, module.spool, module.attempts)
        if ran or spools or attempts:
            self.stdout.write(f"ran {ran} job(s); deleted {len(spools)} spool(s) and {attempts} attempt(s)")
