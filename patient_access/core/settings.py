"""Runtime settings: the snapshot a host's settings provider returns, read once per job attempt.

The library never reads a settings store itself. The host supplies
`Callable[[], PatientAccessSettings]` (JHE reads `JheSetting` rows), the runner calls it once at
the start of an attempt, and the snapshot travels to the fetch (`documents`), the formatter
(`bundle`) and the sink (`s3`). A bad value fails the attempt in its first second, and the next
attempt picks up a fix.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import Field, SecretStr, field_validator, model_validator

from .models import Value

DEFAULT_LAYOUT = "{attrs.patient_id}/{attrs.fhir_source_id}/{created_at:%Y%m%dT%H%M%SZ}/bundle.json"
DEFAULT_EXTENSION_BASE_URL = "https://jupyterhealth.org/fhir/StructureDefinition/"

KiB = 1024
MiB = 1024 * KiB
GiB = 1024 * MiB


class SseMode(StrEnum):
    """Server-side encryption requested on every object."""

    S3 = "AES256"
    KMS = "aws:kms"
    # No header: the bucket's own default encryption applies (on AWS every bucket encrypts with
    # at least SSE-S3). For S3-compatible stores that refuse an explicit header without a KMS, like
    # the local dev store.
    BUCKET_DEFAULT = "bucket-default"


class S3Settings(Value):
    bucket: str = Field(min_length=3, max_length=63)
    endpoint_url: str | None = None  # an S3-compatible store; None for AWS itself
    region: str | None = None
    # Both None: boto3's default chain (the compute's IAM role on AWS).
    access_key_id: str | None = None
    secret_access_key: SecretStr | None = None
    encryption: SseMode = SseMode.S3
    kms_key_id: str | None = None
    layout: str = DEFAULT_LAYOUT

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.kms_key_id and self.encryption is not SseMode.KMS:
            raise ValueError("a KMS key id needs encryption aws:kms")
        if (self.access_key_id is None) != (self.secret_access_key is None):
            raise ValueError("set both the access key id and the secret access key, or neither")
        return self


class DocumentCaps(Value):
    """How much document content a pull may carry. Set very high by default: the partner wants
    everything, and the streaming write keeps memory flat at any size."""

    max_count: int = Field(default=10_000, ge=0)
    max_bytes_each: int = Field(default=2 * GiB, ge=0)
    max_total_bytes: int = Field(default=20 * GiB, ge=0)
    # A server that answers with a FHIR Binary JSON instead of raw bytes is parsed in memory, so
    # that path has its own, lower cap.
    max_binary_json_bytes: int = Field(default=64 * MiB, ge=0)


class BundleSettings(Value):
    """How the delivered document Bundle is labelled."""

    # The namespace of the bundle's custom extensions; a site that publishes its own profiles
    # sets its own.
    extension_base_url: str = DEFAULT_EXTENSION_BASE_URL

    @field_validator("extension_base_url")
    @classmethod
    def _base_url(cls, v: str) -> str:
        if not v.startswith("https://") or not v.endswith("/"):
            raise ValueError("must be an absolute https URL ending in /")
        return v


class PatientAccessSettings(Value):
    s3: S3Settings
    documents: DocumentCaps = DocumentCaps()
    bundle: BundleSettings = BundleSettings()
