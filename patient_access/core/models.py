"""The values core speaks. Hosts translate their own rows into these at the edge.

Two kinds of model, chosen deliberately:

- **Values** (frozen): what a pull is for and where it comes from (`Participant`, `Selection`,
  `Source`, `AuthAttempt`, `TokenResponse`, receipts and document records). Built once, never
  changed; a different value is a new instance.
- **Progress records** (mutable): `Job` and the outcomes on it. The fetch reports progress as it
  goes and the job is saved after every step so a worker that dies leaves an honest row; a
  mutable record saved in place is the simplest faithful model of that.

FHIR resources themselves stay JSON objects (`JsonObject`): validating every resource against a
FHIR model would be slow and would reject vendor quirks we must pass through untouched.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, SecretStr

JobId = str
JsonObject = dict[str, Any]
"""A FHIR resource or other JSON object, passed through as parsed (the one deliberate dict)."""

A = TypeVar("A", bound=BaseModel)
"""The host's participant attributes (JHE: patient, data source, FhirSource ids)."""


class Value(BaseModel):
    """Base for immutable values."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class Record(BaseModel):
    """Base for mutable progress records."""

    model_config = ConfigDict(extra="forbid", validate_assignment=False)


# --- who and where ---------------------------------------------------------------------------


class Participant(Value, Generic[A]):
    """Who a pull is for. `ref` is the host's stable identifier for the person (JHE: the Patient
    pk); `provider` says how the host established it, for the audit trail; `attrs` is the host's
    typed attributes, which the key layout and the bundle's provenance read."""

    ref: str
    provider: str
    attrs: A


class Endpoint(Value):
    """A FHIR server a brand connects through (the SMART `iss`). `vendor` is the host's name for
    the vendor registration the endpoint belongs to; the directory maps it to vendor knowledge.
    `customer_*` is the installation behind the endpoint, when the directory knows it."""

    id: str
    iss: str
    vendor: str
    fhir_version: str | None = None
    customer_id: str | None = None
    customer_name: str | None = None


class Brand(Value):
    """The patient-recognisable organisation (a User-access Brands `Organization`)."""

    id: str
    name: str
    endpoint: Endpoint
    npi: str | None = None


class Facility(Value):
    """A care location under a brand: what the patient searches by."""

    id: str
    name: str
    address: str
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None


class Selection(Value):
    """What the patient picked: a brand, at a facility when it has any. `id` is opaque to core."""

    id: str
    brand: Brand
    facility: Facility | None = None

    @property
    def endpoint(self) -> Endpoint:
        return self.brand.endpoint

    @property
    def name(self) -> str:
        return self.facility.name if self.facility else self.brand.name


# --- phase 3: upstream OAuth -------------------------------------------------------------------


class AuthAttempt(Value, Generic[A]):
    """Server-side state across the EHR redirect. The selection is stored as a value so a
    directory re-import between launch and callback cannot strand the attempt."""

    state: str
    code_verifier: SecretStr
    participant: Participant[A]
    selection: Selection
    scopes: tuple[str, ...]
    scopes_declined: tuple[str, ...] = ()
    redirect_uri: str
    token_endpoint: str
    created_at: datetime
    expires_at: datetime
    # How the launch authenticated, so the callback does the same. Never the credential.
    auth_method: str
    key_id: str | None = None
    scope_version: str
    pkce_advertised: bool = True


class TokenResponse(Value):
    access_token: SecretStr
    expires_at: datetime
    scopes_granted: tuple[str, ...]
    patient: str
    token_type: str = "Bearer"
    refresh_token_issued: bool = False


# --- the job ---------------------------------------------------------------------------------


class JobStatus(StrEnum):
    QUEUED = "queued"
    FETCHING = "fetching"
    FETCHED = "fetched"
    DELIVERING = "delivering"
    DONE = "done"
    FAILED = "failed"  # a delivery failed and will be retried from the spool
    FAILED_TERMINAL = "failed_terminal"  # nothing more will happen; spool deleted

    @property
    def terminal(self) -> bool:
        return self in (JobStatus.DONE, JobStatus.FAILED_TERMINAL)


TERMINAL_STATUSES: frozenset[JobStatus] = frozenset(s for s in JobStatus if s.terminal)


class OutcomeStatus(StrEnum):
    PENDING = "pending"
    OK = "ok"
    ERROR = "error"
    SKIPPED = "skipped"
    NOT_REQUESTED = "not_requested"  # the patient left the type unchecked
    NOT_GRANTED = "not_granted"  # requested; the EHR withheld the scope

    @property
    def withheld(self) -> bool:
        return self in (OutcomeStatus.NOT_REQUESTED, OutcomeStatus.NOT_GRANTED)

    @property
    def settled(self) -> bool:
        return self in (OutcomeStatus.OK, OutcomeStatus.SKIPPED) or self.withheld


class Source(Value):
    """Where the data comes from, fixed when the token arrives. Carries the endpoint's fields as
    values so the worker can look settings up again without a directory row that may be gone."""

    vendor: str
    iss: str
    endpoint_id: str
    fhir_version: str | None = None
    customer_id: str | None = None
    customer_name: str | None = None
    brand_id: str
    brand_name: str
    brand_npi: str | None = None
    facility_id: str | None = None
    facility_name: str | None = None
    ehr_patient_id: str
    scopes_requested: tuple[str, ...]
    scopes_granted: tuple[str, ...]
    scopes_declined: tuple[str, ...] = ()
    auth_method: str
    key_id: str | None = None

    @classmethod
    def of(cls, attempt: AuthAttempt[A], token: TokenResponse) -> Source:
        endpoint = attempt.selection.endpoint
        return cls(
            vendor=endpoint.vendor,
            iss=endpoint.iss,
            endpoint_id=endpoint.id,
            fhir_version=endpoint.fhir_version,
            customer_id=endpoint.customer_id,
            customer_name=endpoint.customer_name,
            brand_id=attempt.selection.brand.id,
            brand_name=attempt.selection.brand.name,
            brand_npi=attempt.selection.brand.npi,
            facility_id=attempt.selection.facility.id if attempt.selection.facility else None,
            facility_name=attempt.selection.facility.name if attempt.selection.facility else None,
            ehr_patient_id=token.patient,
            scopes_requested=attempt.scopes,
            scopes_granted=token.scopes_granted,
            scopes_declined=attempt.scopes_declined,
            auth_method=attempt.auth_method,
            key_id=attempt.key_id,
        )

    @property
    def endpoint(self) -> Endpoint:
        return Endpoint(
            id=self.endpoint_id,
            iss=self.iss,
            vendor=self.vendor,
            fhir_version=self.fhir_version,
            customer_id=self.customer_id,
            customer_name=self.customer_name,
        )

    @property
    def id(self) -> str:
        """A stable, path-safe identifier for the health system: the brand's NPI when known,
        else the vendor plus a hash of the FHIR base URL."""
        if self.brand_npi:
            return f"npi-{self.brand_npi}"
        digest = hashlib.sha256(self.iss.rstrip("/").encode()).hexdigest()[:12]
        return f"{self.vendor}-{digest}"


class TypeOutcome(Record):
    """Phase 4's result for one row of the vendor's query table. Every row appears, so the
    receipt says what was withheld as well as what was fetched."""

    label: str
    resource_type: str
    query: str
    status: OutcomeStatus = OutcomeStatus.PENDING
    count: int = 0
    pages: int = 0
    bytes: int = 0
    error: str | None = None
    capped: bool = False
    seconds: float = 0.0

    def reset(self) -> None:
        self.status, self.count, self.pages, self.bytes = OutcomeStatus.PENDING, 0, 0, 0
        self.error, self.capped, self.seconds = None, False, 0.0


# --- documents -------------------------------------------------------------------------------


class DocumentSource(StrEnum):
    """Where a candidate document was found."""

    DOCUMENT_REFERENCE = "DocumentReference.content"
    DIAGNOSTIC_REPORT = "DiagnosticReport.presentedForm"

    @property
    def resource_type(self) -> str:
        return self.value.split(".")[0]


class DocumentResult(StrEnum):
    STORED = "stored"
    SKIPPED = "skipped"


class SkipReason(StrEnum):
    COUNT_CAP = "count_cap"
    SIZE_CAP_EACH = "size_cap_each"
    SIZE_CAP_TOTAL = "size_cap_total"
    BINARY_JSON_CAP = "binary_json_cap"  # a FHIR Binary answer over the (lower) buffered cap
    NO_CONTENT = "no_content"  # neither data nor url
    BAD_INLINE_DATA = "bad_inline_data"
    URL_OUTSIDE_SERVER = "url_outside_server"  # never send the EHR token to another host
    HTTP_ERROR = "http_error"
    NETWORK = "network"
    UNREADABLE_BINARY = "unreadable_binary"
    TOKEN_EXPIRED = "token_expired"


class DocumentRecord(Value):
    """One candidate attachment and what happened to it. Kept on the job and written into the
    bundle's provenance, so a coordinator can see which note is missing and why."""

    blob_id: str
    source: DocumentSource
    referenced_by: str  # "DocumentReference/123"
    attachment_index: int
    content_type: str | None
    result: DocumentResult
    reason: SkipReason | None = None
    detail: str | None = None  # e.g. the HTTP status; never response text
    size: int | None = None
    sha256: str | None = None


class DocumentOutcome(Record):
    """The documents pass: one record per candidate. Counts are derived, never stored."""

    status: OutcomeStatus = OutcomeStatus.PENDING
    records: list[DocumentRecord] = Field(default_factory=list)
    error: str | None = None

    @property
    def stored(self) -> list[DocumentRecord]:
        return [r for r in self.records if r.result is DocumentResult.STORED]

    @property
    def skipped(self) -> Counter[SkipReason]:
        return Counter(r.reason for r in self.records if r.reason is not None)

    @property
    def bytes(self) -> int:
        return sum(r.size or 0 for r in self.stored)

    def record(self, rec: DocumentRecord) -> None:
        """Add or replace the record for a blob id (a resumed pass replaces its earlier record)."""
        self.records = [r for r in self.records if r.blob_id != rec.blob_id] + [rec]


# --- delivery --------------------------------------------------------------------------------


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    DELIVERING = "delivering"
    DONE = "done"
    FAILED = "failed"  # will be retried
    FAILED_TERMINAL = "failed_terminal"


class PendingUpload(Value):
    """A multipart upload that was started and not yet completed or aborted. Recorded on the
    job so a resumed or failed job aborts it: orphan parts are billed until aborted."""

    bucket: str
    key: str
    upload_id: str


class S3Receipt(Value):
    bucket: str
    key: str
    etag: str
    version_id: str | None = None
    bytes: int
    sha256: str
    parts: int

    @property
    def location(self) -> str:
        return f"s3://{self.bucket}/{self.key}"


class Delivery(Record):
    status: DeliveryStatus = DeliveryStatus.PENDING
    attempts: int = 0
    receipt: S3Receipt | None = None
    failure: str | None = None
    pending_upload: PendingUpload | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class Credentials(Record):
    """A job's secrets: the EHR access token and the spool key. Encrypted at rest by the job
    store, excluded from every dump of the job, and cleared when the job reaches a terminal
    state (which crypto-shreds anything the spool left behind)."""

    ehr_access_token: SecretStr | None = None
    spool_key: SecretStr | None = None

    def clear(self) -> None:
        self.ehr_access_token = None
        self.spool_key = None


class Job(Record, Generic[A]):
    id: JobId
    participant: Participant[A]
    source: Source
    status: JobStatus = JobStatus.QUEUED
    credentials: Credentials = Field(default_factory=Credentials, exclude=True)
    token_expires_at: datetime | None = None
    outcomes: list[TypeOutcome] = Field(default_factory=list)
    documents: DocumentOutcome = Field(default_factory=DocumentOutcome)
    delivery: Delivery = Field(default_factory=Delivery)
    spool_ref: str | None = None
    failure: str | None = None
    runs: int = 0  # how many times a worker has picked the job up
    created_at: datetime
    updated_at: datetime | None = None
    finished_at: datetime | None = None

    def token_live(self, now: datetime) -> bool:
        return self.credentials.ehr_access_token is not None and (
            self.token_expires_at is None or self.token_expires_at > now
        )


# --- spool -----------------------------------------------------------------------------------


class BlobMeta(Value):
    content_type: str
    size: int
    sha256: str
    referenced_by: str
    source_url: str | None = None


class SpoolIndex(Value):
    """What a spool holds: a count per resource type and every committed blob's metadata."""

    types: Mapping[str, int] = Field(default_factory=dict)
    blobs: Mapping[str, BlobMeta] = Field(default_factory=dict)
    bytes: int = 0
