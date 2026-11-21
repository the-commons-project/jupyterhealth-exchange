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


def cidr_list(value: object) -> None:
    """Blank (no restriction) or CIDR blocks / addresses separated by commas or spaces."""
    from core.middleware import parse_networks

    if not isinstance(value, str):
        raise ValueError("must be text")
    try:
        parse_networks(value)
    except ValueError as e:
        raise ValueError(f"has an entry that is not an IP address or CIDR block ({e})") from None


def email_list(value: object) -> None:
    """Blank (no restriction) or email addresses separated by commas or spaces."""
    if not isinstance(value, str):
        raise ValueError("must be text")
    bad = [e for e in value.replace(",", " ").split() if "@" not in e.strip("@") or e.count("@") != 1]
    if bad:
        raise ValueError(f"has an entry that is not an email address ({bad[0]})")


CORE_SETTINGS: tuple[SettingSpec, ...] = (
    SettingSpec(
        key="site.url",
        value_type=SettingValueType.STRING,
        help="Public base URL of this deployment; invitation links, emails and OAuth redirect URIs are built on it.",
        example="https://jhe.example.org",
        validate=site_base_url,
    ),
    SettingSpec(
        key="auth.sso.required_for_practitioners",
        value_type=SettingValueType.BOOL,
        help=(
            "When on, practitioners, staff and superusers can sign in only through SAML SSO; the password and "
            "email-code forms refuse them. Patients are unaffected. Turn it on after each practitioner has "
            "signed in through SSO once."
        ),
        example="true",
    ),
    SettingSpec(
        key="auth.admin.allowed_cidrs",
        value_type=SettingValueType.STRING,
        help=(
            "Networks allowed to reach /admin/ (the Django admin, login included) and the settings API, as CIDR "
            "blocks separated by commas or spaces. Empty means no restriction. If you lock yourself out, set "
            "it with manage.py shell."
        ),
        example="203.0.113.0/24, 198.51.100.7",
        validate=cidr_list,
    ),
    SettingSpec(
        key="auth.practitioner_clients.allowed_emails",
        value_type=SettingValueType.STRING,
        help=(
            "Practitioners whose API keys (client credentials) may be created and used, as emails separated by "
            "commas or spaces. Empty means every practitioner. When set, other practitioners' keys stop getting "
            "tokens, tokens already issued to them stop working, and they cannot create keys."
        ),
        example="jhe-integration@example.org",
        validate=email_list,
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
