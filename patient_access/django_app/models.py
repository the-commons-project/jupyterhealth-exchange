"""The module's two tables. Each row keeps the core model as JSON (`body`, the one source of truth
for its shape) beside the columns that are queried or secret: status and lease for claiming, the
participant ref for "this patient's job", the encrypted secrets."""

from __future__ import annotations

import uuid

from django.db import models


def _uuid() -> str:
    return uuid.uuid4().hex


class AuthAttemptRecord(models.Model):
    """Phase 3: a pending EHR sign-in, from the redirect to the callback. Single use."""

    state = models.CharField(primary_key=True, max_length=64)
    code_verifier_enc = models.TextField()
    body = models.JSONField()  # the AuthAttempt, minus the verifier
    participant_ref = models.CharField(max_length=64, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)
    used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "patient access auth attempt"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"attempt for participant {self.participant_ref}"


class JobRecord(models.Model):
    """One pull, from the callback to the end of delivery."""

    id = models.CharField(primary_key=True, max_length=32, default=_uuid)
    status = models.CharField(max_length=20, db_index=True)
    participant_ref = models.CharField(max_length=64, db_index=True)
    body = models.JSONField()  # the Job, minus its credentials
    credentials_enc = models.TextField(blank=True, default="")  # EHR token and spool key, encrypted
    spool_ref = models.CharField(max_length=200, null=True, blank=True)
    created_at = models.DateTimeField()
    updated_at = models.DateTimeField(auto_now=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    # The runner's lease: written only by claim / renew / release, never by save.
    lease_owner = models.CharField(max_length=200, null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "patient access job"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "lease_expires_at"], name="pa_jobs_status_lease")]

    def __str__(self) -> str:
        return f"job {self.id} ({self.status})"
