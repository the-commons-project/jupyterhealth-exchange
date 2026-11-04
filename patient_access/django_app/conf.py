"""The module's deploy-time configuration: a `PATIENT_ACCESS` dict in the project's settings.py,
validated into `ModuleConfig`. Runtime values (bucket, credentials, caps, layout, extension base
URL) are not here: the host's settings provider supplies them per job attempt.

    PATIENT_ACCESS = {
        "ENCRYPTION_KEYS": [...],          # Fernet keys, first encrypts; empty: the module is off
        "SPOOL_DIR": "/data/patient-access/spool",
        "DISPATCHER": "thread",            # or "none" (the cron sweep alone runs jobs)
        "LEASE_SECONDS": 60,
        "MAX_ATTEMPTS": 3,                 # delivery attempts before a job fails for good
        "LAUNCH_ATTRS": "host.module.LaunchAttrs",           # pydantic model on the auth attempt
        "JOB_ATTRS": "host.module.JobAttrs",                 # pydantic model on the job
        "DIRECTORY": "host.module.Directory",                # class, constructed with no arguments
        "SETTINGS_PROVIDER": "host.module.read_settings",    # () -> PatientAccessSettings
        "DELIVERY_GUARD": "host.module.guard",               # (Job) -> None, raises DeliveryRefused
        "S3_CLIENT_FACTORY": "patient_access.destinations.s3.boto3_client",
        "PART_SIZE": 33554432,
    }

Empty `ENCRYPTION_KEYS` (the variable unset) leaves the module off: a deployment that does not
use it boots as before, and the system check says so. Anything else that is wrong fails startup.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any

from django.conf import settings
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from patient_access.destinations.multipart import DEFAULT_PART, MIN_PART


class ConfigError(Exception):
    pass


class DispatcherKind(StrEnum):
    THREAD = "thread"  # run the job in a thread of the web process, right after the callback
    NONE = "none"  # leave it to the cron sweep


class ModuleConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    encryption_keys: tuple[SecretStr, ...] = Field(min_length=1)
    spool_dir: str = "/data/patient-access/spool"
    dispatcher: DispatcherKind = DispatcherKind.THREAD
    lease_seconds: int = Field(default=60, ge=15)
    max_attempts: int = Field(default=3, ge=1)
    launch_attrs: str
    job_attrs: str
    directory: str
    settings_provider: str
    delivery_guard: str
    s3_client_factory: str = "patient_access.destinations.s3.boto3_client"
    part_size: int = Field(default=DEFAULT_PART, ge=MIN_PART)


def project_settings() -> Mapping[str, Any]:
    value = getattr(settings, "PATIENT_ACCESS", None) or {}
    if not isinstance(value, Mapping):
        raise ConfigError("PATIENT_ACCESS must be a dict")
    return value


def enabled(raw: Mapping[str, Any]) -> bool:
    return bool(raw.get("ENCRYPTION_KEYS"))


def parse(raw: Mapping[str, Any]) -> ModuleConfig:
    try:
        return ModuleConfig.model_validate({k.lower(): v for k, v in raw.items()})
    except ValidationError as e:
        problems = "; ".join(f"{'.'.join(str(p) for p in err['loc']).upper()}: {err['msg']}" for err in e.errors())
        raise ConfigError(f"PATIENT_ACCESS: {problems}") from None
