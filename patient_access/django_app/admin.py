"""Read-only admin pages, so an operator can see a pull's state and failures without the database."""

from __future__ import annotations

from typing import Any

from django.contrib import admin
from django.http import HttpRequest

from .models import AuthAttemptRecord, JobRecord


class _ReadOnly(admin.ModelAdmin):
    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


@admin.register(JobRecord)
class JobRecordAdmin(_ReadOnly):
    list_display = ("id", "status", "participant_ref", "brand", "delivery_attempts", "object_key", "created_at")
    list_filter = ("status",)
    search_fields = ("id", "participant_ref")
    exclude = ("credentials_enc",)
    readonly_fields = ("failure",)

    @admin.display(description="health system")
    def brand(self, obj: JobRecord) -> str:
        return str((obj.body.get("source") or {}).get("brand_name") or "")

    @admin.display(description="delivery attempts")
    def delivery_attempts(self, obj: JobRecord) -> int:
        return int((obj.body.get("delivery") or {}).get("attempts") or 0)

    @admin.display(description="object key")
    def object_key(self, obj: JobRecord) -> str:
        receipt = (obj.body.get("delivery") or {}).get("receipt") or {}
        return str(receipt.get("key") or "")

    @admin.display(description="failure")
    def failure(self, obj: JobRecord) -> str:
        return str(obj.body.get("failure") or "")


@admin.register(AuthAttemptRecord)
class AuthAttemptRecordAdmin(_ReadOnly):
    list_display = ("state", "participant_ref", "created_at", "expires_at", "used_at")
    search_fields = ("participant_ref",)
    exclude = ("code_verifier_enc",)
