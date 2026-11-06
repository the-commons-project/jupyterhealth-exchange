"""The library's runtime settings, read from JheSetting rows, and the registry entries that
validate those rows when an operator saves them (PR 1's registry).

`read_patient_access_settings` is the library's settings provider: the runner calls it once per
job attempt, so an operator's fix is picked up by the next attempt. Every key has a default
except the bucket.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from django.conf import settings
from pydantic import SecretStr, ValidationError

from core.services.jhe_settings import SettingSpec, SettingValueType, get_setting, register_setting
from patient_access.core.credentials import AuthMethodName
from patient_access.core.models import Job, Participant, Source
from patient_access.core.settings import (
    DEFAULT_EXTENSION_BASE_URL,
    DEFAULT_LAYOUT,
    MAX_ATTACHMENT_SIZE,
    BundleSettings,
    DocumentCaps,
    PatientAccessSettings,
    S3Settings,
    SseMode,
)
from patient_access.destinations.layout import LayoutError, TemplateLayout

from .identity import PROVIDER, JheAttrs

PREFIX = "patient_access"
_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_REGION = re.compile(r"^[a-z0-9-]{2,32}$")

HOST_NAME = "jupyterhealth-exchange"  # recorded beside the library in the bundle's provenance


def _text(key: str) -> str | None:
    value = get_setting(f"{PREFIX}.{key}")
    return value.strip() or None if isinstance(value, str) else None


def _int(key: str, default: int) -> int:
    value = get_setting(f"{PREFIX}.{key}")
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def read_patient_access_settings() -> PatientAccessSettings:
    """Raises ValueError (the bucket is not set) or pydantic's ValidationError (a cross-key rule)."""
    bucket = _text("s3.bucket")
    if bucket is None:
        raise ValueError(f"{PREFIX}.s3.bucket is not set")
    secret = _text("s3.secret_access_key")
    caps = DocumentCaps()
    return PatientAccessSettings(
        s3=S3Settings(
            bucket=bucket,
            endpoint_url=_text("s3.endpoint_url"),
            region=_text("s3.region"),
            access_key_id=_text("s3.access_key_id"),
            secret_access_key=SecretStr(secret) if secret else None,
            encryption=SseMode(_text("s3.encryption") or SseMode.S3.value),
            kms_key_id=_text("s3.kms_key_id"),
            layout=_text("s3.layout") or DEFAULT_LAYOUT,
        ),
        documents=DocumentCaps(
            max_count=_int("documents.max_count", caps.max_count),
            max_bytes_each=_int("documents.max_bytes_each", caps.max_bytes_each),
            max_total_bytes=_int("documents.max_total_bytes", caps.max_total_bytes),
        ),
        bundle=BundleSettings(
            extension_base_url=_text("fhir.extension_base_url") or DEFAULT_EXTENSION_BASE_URL,
            host_name=HOST_NAME,
            host_version=settings.JHE_VERSION,
        ),
    )


# --- validation at save time -----------------------------------------------------------------


def _fixture_job() -> Job[JheAttrs]:
    """A pull with every field the layout may name, for dry-rendering a template."""
    now = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    return Job[JheAttrs](
        id="0123456789abcdef0123456789abcdef",
        participant=Participant[JheAttrs](
            ref="40001",
            provider=PROVIDER,
            attrs=JheAttrs(
                patient_id=40001,
                data_source_id=3,
                fhir_source_id=7,
                fhir_source_uri="https://jupyterhealth.org/fhir/fhir-source/7",
                study_ids=(1,),
            ),
        ),
        source=Source(
            vendor="Epic Production",
            iss="https://fhir.example.org/api/FHIR/R4",
            endpoint_id="1",
            brand_id="1",
            brand_name="Example Health",
            facility_id="1",
            facility_name="Example Clinic",
            ehr_patient_id="eXAMPLE",
            scopes_requested=(),
            scopes_granted=(),
            auth_method=AuthMethodName.PUBLIC,
        ),
        created_at=now,
        token_expires_at=now + timedelta(hours=1),
    )


def validate_layout(value: Any) -> None:
    if not isinstance(value, str):
        raise ValueError("must be text")
    try:
        TemplateLayout(value).render(_fixture_job())
    except LayoutError as e:
        raise ValueError(str(e)) from None


def _validate_bucket(value: Any) -> None:
    if not isinstance(value, str) or not _BUCKET.match(value):
        raise ValueError("must be an S3 bucket name: 3-63 lowercase letters, digits, dots and hyphens")


def _validate_optional_url(value: Any) -> None:
    if value and not (isinstance(value, str) and re.match(r"^https?://[^\s/]+", value)):
        raise ValueError("must be an http(s) URL, or empty for AWS itself")


def _validate_region(value: Any) -> None:
    if value and not (isinstance(value, str) and _REGION.match(value)):
        raise ValueError("must be a region name such as us-east-1, or empty")


def _validate_encryption(value: Any) -> None:
    if value not in {m.value for m in SseMode}:
        raise ValueError(f"must be one of {', '.join(m.value for m in SseMode)}")


def _validate_count(value: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("must be a whole number of 0 or more")


def _validate_document_size(value: Any) -> None:
    _validate_count(value)
    if value > MAX_ATTACHMENT_SIZE:
        raise ValueError(f"must be at most {MAX_ATTACHMENT_SIZE} (FHIR R4 sizes are 32-bit)")


def _validate_extension_base_url(value: Any) -> None:
    try:
        BundleSettings(extension_base_url=value)
    except ValidationError:
        raise ValueError("must be an absolute https URL ending in /") from None


_STRING, _INT = SettingValueType.STRING, SettingValueType.INT
_CAPS = DocumentCaps()

SPECS: tuple[SettingSpec, ...] = (
    SettingSpec(
        key=f"{PREFIX}.s3.bucket",
        value_type=_STRING,
        help="USCDI Patient Access: the S3 bucket every pull is delivered to (one per deployment).",
        example="study-ehr-pulls",
        validate=_validate_bucket,
    ),
    SettingSpec(
        key=f"{PREFIX}.s3.endpoint_url",
        value_type=_STRING,
        help="USCDI Patient Access: an S3-compatible endpoint; leave empty for AWS itself.",
        example="http://s3:9000",
        validate=_validate_optional_url,
    ),
    SettingSpec(
        key=f"{PREFIX}.s3.region",
        value_type=_STRING,
        help="USCDI Patient Access: the bucket's region; leave empty for the default.",
        example="us-east-1",
        validate=_validate_region,
    ),
    SettingSpec(
        key=f"{PREFIX}.s3.access_key_id",
        value_type=_STRING,
        help="USCDI Patient Access: access key id; leave both keys empty to use the machine's IAM role.",
        example="AKIAEXAMPLE",
    ),
    SettingSpec(
        key=f"{PREFIX}.s3.secret_access_key",
        value_type=_STRING,
        help="USCDI Patient Access: secret access key; leave both keys empty to use the machine's IAM role.",
        example="(secret)",
    ),
    SettingSpec(
        key=f"{PREFIX}.s3.encryption",
        value_type=_STRING,
        help=(
            "USCDI Patient Access: server-side encryption requested for every object: AES256, aws:kms, or "
            "bucket-default (no header; the bucket's own default encryption applies). Use bucket-default only "
            "with a bucket that encrypts by default (every AWS bucket does): on an S3-compatible store without "
            "default encryption it stores the bundles unencrypted."
        ),
        example="AES256",
        validate=_validate_encryption,
    ),
    SettingSpec(
        key=f"{PREFIX}.s3.kms_key_id",
        value_type=_STRING,
        help="USCDI Patient Access: the KMS key for aws:kms encryption; leave empty for the bucket's default key.",
        example="arn:aws:kms:us-east-1:111122223333:key/1234abcd-12ab-34cd-56ef-1234567890ab",
    ),
    SettingSpec(
        key=f"{PREFIX}.s3.layout",
        value_type=_STRING,
        help=(
            "USCDI Patient Access: the object key, as a template. Fields: {attrs.patient_id}, "
            "{attrs.data_source_id}, {attrs.fhir_source_id}, {job.id}, {participant.ref}, {source.id}, "
            "{source.vendor}, {source.brand_name}, {source.facility_id}, {created_at:<strftime>}. It must "
            "contain {job.id} or a {created_at...} field, so a second pull never overwrites the first."
        ),
        example=DEFAULT_LAYOUT,
        validate=validate_layout,
    ),
    SettingSpec(
        key=f"{PREFIX}.documents.max_count",
        value_type=_INT,
        help="USCDI Patient Access: the most documents one pull embeds.",
        example=str(_CAPS.max_count),
        validate=_validate_count,
    ),
    SettingSpec(
        key=f"{PREFIX}.documents.max_bytes_each",
        value_type=_INT,
        help=(
            f"USCDI Patient Access: the largest single document embedded, in bytes; at most {MAX_ATTACHMENT_SIZE} "
            "(FHIR R4 sizes are 32-bit)."
        ),
        example=str(_CAPS.max_bytes_each),
        validate=_validate_document_size,
    ),
    SettingSpec(
        key=f"{PREFIX}.documents.max_total_bytes",
        value_type=_INT,
        help="USCDI Patient Access: the most document bytes one pull embeds.",
        example=str(_CAPS.max_total_bytes),
        validate=_validate_count,
    ),
    SettingSpec(
        key=f"{PREFIX}.fhir.extension_base_url",
        value_type=_STRING,
        help=(
            "USCDI Patient Access: the namespace of the delivered bundle's custom FHIR extensions "
            "(provenance and fetch outcomes). A site that publishes its own profiles sets its own."
        ),
        example=DEFAULT_EXTENSION_BASE_URL,
        validate=_validate_extension_base_url,
    ),
)


def register_settings() -> None:
    for spec in SPECS:
        register_setting(spec)
