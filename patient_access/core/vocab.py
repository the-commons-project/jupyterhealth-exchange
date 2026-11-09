"""Fixed vocabularies shared by the models and the modules that act on them.

They live here, importing nothing, so `models` can type its fields with them without importing
the modules that use the models (`credentials`, `source_settings`).
"""

from __future__ import annotations

from enum import StrEnum


class AuthMethodName(StrEnum):
    PUBLIC = "public"
    CLIENT_SECRET_BASIC = "client_secret_basic"
    CLIENT_SECRET_POST = "client_secret_post"
    PRIVATE_KEY_JWT = "private_key_jwt"


class ScopeVersion(StrEnum):
    V1 = "v1"  # patient/X.read
    V2 = "v2"  # patient/X.rs


class FailureKind(StrEnum):
    """What a failed job asks of the patient. Decides the patient-facing copy (the host maps each
    kind to its own words) and whether anything will happen without them."""

    RETRY_LATER = "retry_later"  # ours or the hospital's side was unavailable; retried automatically
    RECONNECT = "reconnect"  # the hospital's authorization ended or was refused; sign in again
    CONTACT_STUDY = "contact_study"  # retries are exhausted, or the service cannot proceed
    CONSENT_WITHDRAWN = "consent_withdrawn"  # the host's guard refused delivery; nothing to do


class TypeChoice(StrEnum):
    """Where the patient chooses which record types to share.

    HOST: the host shows its own checklist and narrows the requested scopes to the ticked types.
    EHR: the vendor's authorization screen asks per type itself, from the app registration, and
    ignores the narrowing (Epic, observed 2026-11-04: every registered category is listed, ticked,
    whatever the request asked for). The host should not ask twice: it requests every type and
    the patient's choice shows up as scopes the EHR did not grant.
    """

    HOST = "host"
    EHR = "ehr"
