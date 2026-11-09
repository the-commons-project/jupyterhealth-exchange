"""Read-only admin pages, so an operator can see a pull's state and failures without the database.

Rows are read through the job model, not as raw JSON, so the pages show what the code means.
A running job's row cannot be deleted (its worker would lose it mid-pull), and sign-in attempts
are listed without their `state`, which is a live single-use secret until the callback spends it:
no column, no change page and no bulk actions, since each of those would put it in the HTML.
"""

from __future__ import annotations

from typing import Any

from django.contrib import admin
from django.http import HttpRequest
from pydantic import BaseModel, ValidationError

from patient_access.core.models import TERMINAL_STATUSES, Job

from .models import AuthAttemptRecord, JobRecord
from .wiring import NotConfigured, get_module

_TERMINAL = frozenset(s.value for s in TERMINAL_STATUSES)


class _ReadOnly(admin.ModelAdmin):
    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


def _job(row: JobRecord) -> Job[BaseModel] | None:
    """The row as a job, or None when the module is off here or the row no longer loads."""
    try:
        return get_module().jobs.view(row)
    except (NotConfigured, ValidationError):
        return None


@admin.register(JobRecord)
class JobRecordAdmin(_ReadOnly):
    list_display = ("id", "status", "participant_ref", "brand", "delivery_attempts", "object_key", "created_at")
    list_filter = ("status",)
    search_fields = ("id", "participant_ref")
    exclude = ("credentials_enc",)
    readonly_fields = ("failure",)

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        """Django's own permission check, narrowed: never a job that is still running."""
        return super().has_delete_permission(request, obj) and (obj is None or obj.status in _TERMINAL)

    @admin.display(description="health system")
    def brand(self, obj: JobRecord) -> str:
        job = _job(obj)
        return job.source.brand_name if job else ""

    @admin.display(description="delivery attempts")
    def delivery_attempts(self, obj: JobRecord) -> int:
        job = _job(obj)
        return job.delivery.attempts if job else 0

    @admin.display(description="object key")
    def object_key(self, obj: JobRecord) -> str:
        job = _job(obj)
        return job.delivery.receipt.key if job and job.delivery.receipt else ""

    @admin.display(description="failure")
    def failure(self, obj: JobRecord) -> str:
        job = _job(obj)
        if job is None:
            return "(the row could not be read as a job)"
        if job.failure is None:
            return ""
        return f"{job.failure} [{job.failure_kind.value}]" if job.failure_kind else job.failure


@admin.register(AuthAttemptRecord)
class AuthAttemptRecordAdmin(_ReadOnly):
    list_display = ("participant_ref", "created_at", "expires_at", "used_at")
    list_display_links = None  # no change page: its URL would carry the state
    actions = None  # no bulk-action checkboxes: their values are the rows' keys, the states
    search_fields = ("participant_ref",)
    exclude = ("code_verifier_enc", "state")
