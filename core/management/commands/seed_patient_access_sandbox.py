"""Dev only: put the SMART Health IT sandbox "hospitals" into JHE's EHR tables for USCDI Patient
Access, and point its S3 settings at the local S3 service.

Each sandbox brand's iss encodes the sandbox's launch options (public client, PKCE, scripted
login). Brands and their locations are updated in place by name on every run, never deleted and
recreated: a FhirSource points at its location, and a recreated location would detach it, so the
next pull would start a new FhirSource and a new S3 prefix. Brands no longer in the file are
removed. The S3 settings are written only when PATIENT_ACCESS_DEV_S3_BUCKET is set (the compose
stack sets it), and the bucket is created if the local S3 service is up.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandParser
from django.db import transaction
from pydantic import BaseModel, ConfigDict

from core.models import EhrBrand, EhrBrandLocation, EhrVendor, JheSetting
from core.services.jhe_settings import SettingValueType
from patient_access.testing.smart_sandbox import SandboxConfig

DEFAULT_FILE = Path("core") / "data" / "patient_access_sandbox.json"


class SandboxBrand(BaseModel):
    """One sandbox "hospital" in the file: its name and address, and the sandbox launch options
    its iss encodes (the fields of `SandboxConfig`)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    line: str
    city: str
    state: str
    postal: str
    launch: dict[str, Any]


class SandboxSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    vendor: str
    client_id: str
    brands: tuple[SandboxBrand, ...]


def _set(key: str, value_type: SettingValueType, value: Any) -> None:
    setting, _ = JheSetting.objects.get_or_create(key=key, defaults={"value_type": value_type})
    setting.set_value(value_type, value)
    setting.full_clean()
    setting.save()


class Command(BaseCommand):
    help = "Dev: seed the SMART sandbox hospitals and the local S3 settings for USCDI Patient Access."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--file", default=str(DEFAULT_FILE), help="Sandbox brands JSON (default: %(default)s).")

    def handle(self, *args: Any, **options: Any) -> None:
        spec = SandboxSpec.model_validate(json.loads(Path(options["file"]).read_text()))
        self.seed_brands(spec)
        self.seed_s3_settings()

    @transaction.atomic
    def seed_brands(self, spec: SandboxSpec) -> None:
        vendor, _ = EhrVendor.objects.get_or_create(name=spec.vendor)
        vendor.ehr_client_id = spec.client_id  # the sandbox accepts any client id
        vendor.supported_scopes = " ".join(EhrVendor.SUPPORTED_SCOPES)
        vendor.save()
        names = [b.name for b in spec.brands]
        EhrBrand.objects.filter(vendor=vendor).exclude(name__in=names).delete()
        for b in spec.brands:
            iss = SandboxConfig(client_id=spec.client_id, **b.launch).iss()
            brand, _ = EhrBrand.objects.update_or_create(
                vendor=vendor, name=b.name, defaults={"fhir_base_url": iss, "fhir_version": "4.0.1"}
            )
            EhrBrandLocation.objects.update_or_create(
                brand=brand,
                name=b.name,
                defaults={
                    "address_text": f"{b.line}, {b.city}, {b.state} {b.postal}",
                    "city": b.city,
                    "state": b.state,
                    "postal_code": b.postal,
                    "country": "US",
                },
            )
            self.stdout.write(f"{b.name} ({vendor.name})")

    def seed_s3_settings(self) -> None:
        bucket = os.getenv("PATIENT_ACCESS_DEV_S3_BUCKET")
        if not bucket:
            return
        endpoint = os.getenv("PATIENT_ACCESS_DEV_S3_ENDPOINT_URL", "")
        access_key, secret_key = os.getenv("MINIO_ROOT_USER", ""), os.getenv("MINIO_ROOT_PASSWORD", "")
        _set("patient_access.s3.bucket", SettingValueType.STRING, bucket)
        _set("patient_access.s3.endpoint_url", SettingValueType.STRING, endpoint)
        _set("patient_access.s3.region", SettingValueType.STRING, "us-east-1")
        _set("patient_access.s3.access_key_id", SettingValueType.STRING, access_key)
        _set("patient_access.s3.secret_access_key", SettingValueType.STRING, secret_key)
        # The local store has no KMS, so it refuses an explicit encryption header.
        _set("patient_access.s3.encryption", SettingValueType.STRING, "bucket-default")
        self.stdout.write(f"patient_access.s3.* -> {endpoint or 'AWS'} bucket {bucket}")
        if endpoint:
            self.create_bucket(endpoint, bucket, access_key, secret_key)

    def create_bucket(self, endpoint: str, bucket: str, access_key: str, secret_key: str, attempts: int = 3) -> None:
        import boto3
        from botocore.config import Config
        from botocore.exceptions import BotoCoreError, ClientError

        s3 = boto3.client(
            "s3",
            endpoint_url=endpoint,
            region_name="us-east-1",
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            config=Config(s3={"addressing_style": "path"}, connect_timeout=2, retries={"max_attempts": 1}),
        )
        for n in range(attempts):
            try:
                if bucket not in {b["Name"] for b in s3.list_buckets().get("Buckets", [])}:
                    s3.create_bucket(Bucket=bucket)
                self.stdout.write(f"bucket {bucket} ready")
                return
            except (BotoCoreError, ClientError):
                time.sleep(2)
        self.stdout.write(
            self.style.WARNING(
                f"local S3 at {endpoint} is not reachable; start it (COMPOSE_PROFILES=s3) and rerun this command"
            )
        )
