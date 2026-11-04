"""Dev only: put the SMART Health IT sandbox "hospitals" into JHE's EHR tables for USCDI Patient
Access, and point its S3 settings at the local S3 service.

Each sandbox brand's iss encodes the sandbox's launch options (public client, PKCE, scripted
login), so the brands are rebuilt on every run. The S3 settings are written only when
PATIENT_ACCESS_DEV_S3_BUCKET is set (the compose stack sets it), and the bucket is created if the
local S3 service is up.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from django.core.management.base import BaseCommand
from django.db import transaction

from core.models import EhrBrand, EhrBrandLocation, EhrVendor, JheSetting
from patient_access.testing.smart_sandbox import SandboxConfig

DEFAULT_FILE = Path("core") / "data" / "patient_access_sandbox.json"


def _set(key, value_type, value):
    setting, _ = JheSetting.objects.get_or_create(key=key, defaults={"value_type": value_type})
    setting.set_value(value_type, value)
    setting.full_clean()
    setting.save()


class Command(BaseCommand):
    help = "Dev: seed the SMART sandbox hospitals and the local S3 settings for USCDI Patient Access."

    def add_arguments(self, parser):
        parser.add_argument("--file", default=str(DEFAULT_FILE), help="Sandbox brands JSON (default: %(default)s).")

    def handle(self, *args, **options):
        spec = json.loads(Path(options["file"]).read_text())
        self.seed_brands(spec)
        self.seed_s3_settings()

    @transaction.atomic
    def seed_brands(self, spec):
        vendor, _ = EhrVendor.objects.get_or_create(name=spec["vendor"])
        vendor.ehr_client_id = spec["client_id"]  # the sandbox accepts any client id
        vendor.supported_scopes = " ".join(EhrVendor.SUPPORTED_SCOPES)
        vendor.save()
        EhrBrand.objects.filter(vendor=vendor).delete()
        for b in spec["brands"]:
            iss = SandboxConfig(client_id=spec["client_id"], **b["launch"]).iss()
            brand = EhrBrand.objects.create(name=b["name"], vendor=vendor, fhir_base_url=iss, fhir_version="4.0.1")
            EhrBrandLocation.objects.create(
                brand=brand,
                name=b["name"],
                address_text=f"{b['line']}, {b['city']}, {b['state']} {b['postal']}",
                city=b["city"],
                state=b["state"],
                postal_code=b["postal"],
                country="US",
            )
            self.stdout.write(f"{b['name']} ({vendor.name})")

    def seed_s3_settings(self):
        bucket = os.getenv("PATIENT_ACCESS_DEV_S3_BUCKET")
        if not bucket:
            return
        endpoint = os.getenv("PATIENT_ACCESS_DEV_S3_ENDPOINT_URL", "")
        access_key, secret_key = os.getenv("MINIO_ROOT_USER", ""), os.getenv("MINIO_ROOT_PASSWORD", "")
        _set("patient_access.s3.bucket", "string", bucket)
        _set("patient_access.s3.endpoint_url", "string", endpoint)
        _set("patient_access.s3.region", "string", "us-east-1")
        _set("patient_access.s3.access_key_id", "string", access_key)
        _set("patient_access.s3.secret_access_key", "string", secret_key)
        # The local store has no KMS, so it refuses an explicit encryption header.
        _set("patient_access.s3.encryption", "string", "bucket-default")
        self.stdout.write(f"patient_access.s3.* -> {endpoint or 'AWS'} bucket {bucket}")
        if endpoint:
            self.create_bucket(endpoint, bucket, access_key, secret_key)

    def create_bucket(self, endpoint, bucket, access_key, secret_key, attempts=3):
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
