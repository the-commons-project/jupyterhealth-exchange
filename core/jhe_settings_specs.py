"""The ``JheSetting`` keys the core app owns and chooses to validate.

Only keys whose misconfiguration has bitten, or would bite silently, are registered: the rest of
the seeded keys keep behaving as free-form rows. Registration happens from ``CoreConfig.ready``.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator

from core.services.jhe_settings import SettingSpec, SettingValueType, register_setting

_http_url = URLValidator(schemes=["http", "https"])


def positive_int(value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("must be a whole number of 1 or more")


def site_base_url(value: object) -> None:
    """An absolute http(s) URL with nothing after the host (or host:port) and no trailing slash:
    callers append paths such as ``/auth/callback`` to it verbatim."""
    if not isinstance(value, str):
        raise ValueError("must be text")
    try:
        _http_url(value)
    except ValidationError:
        raise ValueError("must be an absolute http or https URL") from None
    if value.endswith("/"):
        raise ValueError("must not end with a slash")


CORE_SETTINGS: tuple[SettingSpec, ...] = (
    SettingSpec(
        key="site.url",
        value_type=SettingValueType.STRING,
        help="Public base URL of this deployment; invitation links, emails and OAuth redirect URIs are built on it.",
        example="https://jhe.example.org",
        validate=site_base_url,
    ),
    SettingSpec(
        key="auth.patient.invitation_expiration_days",
        value_type=SettingValueType.INT,
        help="How many days an emailed patient invitation link stays valid.",
        example="7",
        validate=positive_int,
    ),
)


def register_core_settings() -> None:
    for spec in CORE_SETTINGS:
        register_setting(spec)
