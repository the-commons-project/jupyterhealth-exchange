"""ORM implementations of the attempt and job stores, claiming included. The edge where rows
become core models: each row's `body` is the model's JSON; secrets are encrypted columns."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Generic, cast

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from pydantic import SecretStr

from patient_access.core.models import (
    TERMINAL_STATUSES,
    A,
    AuthAttempt,
    Credentials,
    Job,
    JobId,
    JobStatus,
)

from .crypto import Crypto
from .models import AuthAttemptRecord, JobRecord

_TERMINAL = [s.value for s in TERMINAL_STATUSES]
_RUNNING = [s.value for s in (JobStatus.QUEUED, JobStatus.FETCHING, JobStatus.FETCHED, JobStatus.DELIVERING)]


class OrmAuthAttemptStore(Generic[A]):
    def __init__(self, attrs_type: type[A], crypto: Crypto) -> None:
        self.model = cast("type[AuthAttempt[A]]", AuthAttempt[attrs_type])  # type: ignore[valid-type]
        self.crypto = crypto

    def create(self, attempt: AuthAttempt[A]) -> None:
        AuthAttemptRecord.objects.create(
            state=attempt.state,
            code_verifier_enc=self.crypto.encrypt(attempt.code_verifier.get_secret_value()),
            body=attempt.model_dump(mode="json", exclude={"code_verifier"}),
            participant_ref=attempt.participant.ref,
            expires_at=attempt.expires_at,
        )

    def consume(self, state: str) -> AuthAttempt[A]:
        now = timezone.now()
        with transaction.atomic():
            spent = AuthAttemptRecord.objects.filter(state=state, used_at__isnull=True, expires_at__gt=now).update(
                used_at=now
            )
            if not spent:
                raise KeyError(state)
            row = AuthAttemptRecord.objects.get(state=state)
        return self.model.model_validate({**row.body, "code_verifier": self.crypto.decrypt(row.code_verifier_enc)})

    def sweep(self, older_than: timedelta) -> int:
        deleted, _ = AuthAttemptRecord.objects.filter(expires_at__lt=timezone.now() - older_than).delete()
        return int(deleted)


class OrmJobStore(Generic[A]):
    def __init__(self, attrs_type: type[A], crypto: Crypto) -> None:
        self.model = cast("type[Job[A]]", Job[attrs_type])  # type: ignore[valid-type]
        self.crypto = crypto

    # --- serde

    def _credentials_enc(self, c: Credentials) -> str:
        values = {
            "ehr_access_token": c.ehr_access_token.get_secret_value() if c.ehr_access_token else None,
            "spool_key": c.spool_key.get_secret_value() if c.spool_key else None,
        }
        return self.crypto.encrypt(json.dumps(values)) if any(values.values()) else ""

    def _credentials(self, token: str) -> Credentials:
        if not token:
            return Credentials()
        values = json.loads(self.crypto.decrypt(token))
        return Credentials(
            ehr_access_token=SecretStr(values["ehr_access_token"]) if values.get("ehr_access_token") else None,
            spool_key=SecretStr(values["spool_key"]) if values.get("spool_key") else None,
        )

    def _job(self, row: JobRecord) -> Job[A]:
        job = self.model.model_validate(row.body)
        job.credentials = self._credentials(row.credentials_enc)
        job.updated_at = row.updated_at
        return job

    def _write(self, job: Job[A], row: JobRecord) -> JobRecord:
        row.status = job.status.value
        row.participant_ref = job.participant.ref
        row.body = job.model_dump(mode="json")
        row.credentials_enc = self._credentials_enc(job.credentials)
        row.spool_ref = job.spool_ref
        row.created_at = job.created_at
        row.finished_at = job.finished_at
        return row

    # --- JobStore

    def create(self, job: Job[A]) -> Job[A]:
        row = JobRecord()
        job = job.model_copy(update={"id": row.id})
        job.credentials = job.credentials.model_copy()
        self._write(job, row).save(force_insert=True)
        job.updated_at = row.updated_at
        return job

    def get(self, job_id: JobId) -> Job[A]:
        try:
            return self._job(JobRecord.objects.get(id=job_id))
        except JobRecord.DoesNotExist:
            raise KeyError(job_id) from None

    def save(self, job: Job[A]) -> None:
        """An UPDATE of the job's columns only: the lease columns belong to claim / renew / release."""
        now = timezone.now()
        row = self._write(job, JobRecord(id=job.id))
        updated = JobRecord.objects.filter(id=job.id).update(
            status=row.status,
            participant_ref=row.participant_ref,
            body=row.body,
            credentials_enc=row.credentials_enc,
            spool_ref=row.spool_ref,
            finished_at=row.finished_at,
            updated_at=now,
        )
        if not updated:
            raise KeyError(job.id)
        job.updated_at = now

    def claim(self, job_id: JobId, worker: str, lease: timedelta) -> bool:
        now = timezone.now()
        return bool(
            JobRecord.objects.filter(id=job_id)
            .exclude(status__in=_TERMINAL)
            .filter(Q(lease_expires_at__isnull=True) | Q(lease_expires_at__lt=now) | Q(lease_owner=worker))
            .update(lease_owner=worker, lease_expires_at=now + lease)
        )

    def claim_next(self, worker: str, lease: timedelta, retry_after: timedelta) -> JobId | None:
        now = timezone.now()
        runnable = Q(status__in=_RUNNING) | Q(status=JobStatus.FAILED.value, updated_at__lt=now - retry_after)
        free = Q(lease_expires_at__isnull=True) | Q(lease_expires_at__lt=now)
        with transaction.atomic():
            row = (
                JobRecord.objects.select_for_update(skip_locked=True)
                .filter(runnable & free)
                .order_by("created_at")
                .only("id")
                .first()
            )
            if row is None:
                return None
            JobRecord.objects.filter(id=row.id).update(lease_owner=worker, lease_expires_at=now + lease)
            return str(row.id)

    def renew(self, job_id: JobId, worker: str, lease: timedelta) -> bool:
        return bool(
            JobRecord.objects.filter(id=job_id, lease_owner=worker).update(lease_expires_at=timezone.now() + lease)
        )

    def release(self, job_id: JobId, worker: str) -> None:
        JobRecord.objects.filter(id=job_id, lease_owner=worker).update(lease_owner=None, lease_expires_at=None)

    def for_participant(self, ref: str, limit: int = 20) -> list[Job[A]]:
        """A participant's jobs, newest first (for the host's "your pulls" screens)."""
        rows = JobRecord.objects.filter(participant_ref=ref).order_by("-created_at")[:limit]
        return [self._job(row) for row in rows]

    def live_spool_refs(self) -> frozenset[str]:
        refs = (
            JobRecord.objects.exclude(status__in=_TERMINAL)
            .exclude(spool_ref__isnull=True)
            .values_list("spool_ref", flat=True)
        )
        return frozenset(str(r) for r in refs if r)
