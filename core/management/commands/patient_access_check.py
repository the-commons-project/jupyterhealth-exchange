"""For operators after changing the USCDI Patient Access settings: can the next pull be delivered?

Reads the settings as a job would, dry-renders the key layout, then probes the bucket: it must
exist, and a zero-byte marker under `_probe/` must be writable and deletable (the only delete the
bucket policy needs to allow). Exits non-zero with the reason when anything fails.
"""

from __future__ import annotations

import uuid
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from pydantic import ValidationError

from core.patient_access.settings import read_patient_access_settings, validate_layout
from patient_access.destinations.s3 import sse_arguments
from patient_access.django_app.wiring import NotConfigured, get_module

PROBE_PREFIX = "_probe/"


class Command(BaseCommand):
    help = "Check that USCDI Patient Access can deliver: settings, key layout, and the bucket."

    def handle(self, *args: Any, **options: Any) -> None:
        try:
            module = get_module()
        except NotConfigured as e:
            raise CommandError(str(e)) from None
        try:
            settings = read_patient_access_settings()
        except (ValueError, ValidationError) as e:
            raise CommandError(f"settings: {e}") from None
        try:
            validate_layout(settings.s3.layout)
        except ValueError as e:
            raise CommandError(f"patient_access.s3.layout: {e}") from None
        self.stdout.write("settings: ok")

        factory = module.deps.sink.client_factory
        client = factory(settings.s3)
        key = f"{PROBE_PREFIX}{uuid.uuid4().hex}"
        try:
            client.head_bucket(Bucket=settings.s3.bucket)
            client.put_object(Bucket=settings.s3.bucket, Key=key, Body=b"", **sse_arguments(settings.s3))
            client.delete_object(Bucket=settings.s3.bucket, Key=key)
        except Exception as e:  # noqa: BLE001 - every failure is the operator's answer
            raise CommandError(f"bucket {settings.s3.bucket}: {type(e).__name__}: {e}") from None
        self.stdout.write(self.style.SUCCESS(f"bucket {settings.s3.bucket}: writable"))
