"""Open Wearables integration service.

Client for stock Open Wearables (the-momentum/open-wearables) endpoints.
All paths use /api/v1/users/ and /api/v1/oauth/ — the stock OW API surface.
No forked /external/ endpoints.

Reads OW config from JheSetting at request time so admins can change
the OW URL/key via System Settings without restarting the server.

Required JheSettings (no setting_id, i.e. global):
    ow.api_base_url             — Base URL of the Open Wearables instance
    ow.api_key                  — API key for X-Open-Wearables-API-Key header
    ow.lookback_days            — sliding window for polling pipeline
    ow.initial_backfill_days    — first-poll window
    ow.ingest_mode              — polling | webhook | disabled
    ow.webhook_secret           — HMAC shared secret (only if mode=webhook)
"""

import logging
from urllib.parse import urlparse

import requests
from django.core.exceptions import ImproperlyConfigured

from core.jhe_settings.service import get_setting

logger = logging.getLogger(__name__)


def _get_ow_config() -> tuple[str, str]:
    """Read OW base URL and API key from JheSettings."""
    base_url = get_setting("ow.api_base_url")
    api_key = get_setting("ow.api_key")
    if not base_url or not api_key:
        raise ValueError(
            "Open Wearables settings missing: configure ow.api_base_url and "
            "ow.api_key in System Settings"
        )
    return base_url.rstrip("/"), api_key


def _headers() -> dict:
    _, api_key = _get_ow_config()
    return {"X-Open-Wearables-API-Key": api_key}


def _base_url() -> str:
    base_url, _ = _get_ow_config()
    return base_url


class OWIntegrationService:
    """Client for stock Open Wearables API (the-momentum/open-wearables)."""

    # ---- User management ----

    def find_or_create_user(
        self,
        email: str,
        *,
        first_name: str | None = None,
        last_name: str | None = None,
        external_user_id: str | None = None,
    ) -> str:
        """POST /api/v1/users — create a user. Returns the OW user ID.

        Stock OW returns 201 on create, 409 if email already exists. On 409
        we look up the existing user by email. Optional fields improve
        correlation in the OW dashboard.
        """
        payload: dict[str, str] = {"email": email}
        if first_name:
            payload["first_name"] = first_name
        if last_name:
            payload["last_name"] = last_name
        if external_user_id:
            payload["external_user_id"] = external_user_id
        response = requests.post(
            f"{_base_url()}/api/v1/users",
            headers=_headers(),
            json=payload,
            timeout=30,
        )
        if response.status_code == 201:
            data = response.json()
            return str(data.get("id"))
        elif response.status_code == 409:
            # User already exists — look up by email
            lookup = requests.get(
                f"{_base_url()}/api/v1/users",
                headers=_headers(),
                params={"email": email},
                timeout=30,
            )
            lookup.raise_for_status()
            users = lookup.json()
            items = users.get("items", users) if isinstance(users, dict) else users
            if isinstance(items, list) and items:
                return str(items[0].get("id"))
            raise ValueError(f"User with email {email} exists in OW but lookup returned no results")
        else:
            response.raise_for_status()
            return ""  # unreachable, raise_for_status throws

    # ---- OAuth / provider ----

    def list_providers(self) -> list:
        """GET /api/v1/oauth/providers — return enabled providers."""
        response = requests.get(
            f"{_base_url()}/api/v1/oauth/providers",
            headers=_headers(),
            params={"enabled_only": "true", "cloud_only": "true"},
            timeout=30,
        )
        response.raise_for_status()
        return response.json()

    def get_wearable_auth_url(self, ow_user_id: str, provider: str, redirect_uri: str) -> str:
        """GET /api/v1/oauth/{provider}/authorize — returns the auth URL.

        Stock OW's authorize endpoint is a GET that returns a redirect (302)
        or a JSON body with the authorization URL depending on the accept
        header. We request JSON and extract the URL.
        """
        response = requests.get(
            f"{_base_url()}/api/v1/oauth/{provider}/authorize",
            headers=_headers(),
            params={"user_id": ow_user_id, "redirect_uri": redirect_uri},
            timeout=30,
            allow_redirects=False,
        )
        # Stock OW returns 302 with Location header
        if response.status_code == 302:
            return response.headers["Location"]
        # Or it might return 200 with JSON
        if response.ok:
            data = response.json()
            return data.get("authorization_url") or data.get("url") or data.get("redirect_url", "")
        response.raise_for_status()
        return ""  # unreachable

    # ---- Connection management ----

    def check_connection_status(self, ow_user_id: str) -> list:
        """GET /api/v1/users/{id}/connections — list active connections."""
        response = requests.get(
            f"{_base_url()}/api/v1/users/{ow_user_id}/connections",
            headers=_headers(),
            timeout=30,
        )
        response.raise_for_status()
        return response.json()

    def revoke_connection(self, ow_user_id: str, provider: str = "oura") -> None:
        """DELETE /api/v1/users/{id}/connections/{provider}.

        Best-effort: caller catches RequestException and treats failure as
        non-fatal (JHE consent is already revoked).
        """
        response = requests.delete(
            f"{_base_url()}/api/v1/users/{ow_user_id}/connections/{provider}",
            headers=_headers(),
            timeout=30,
        )
        response.raise_for_status()

    # ---- Polling pipeline (v1) helpers ----

    @staticmethod
    def _unwrap_data_list(body):
        """OW responses are either a bare list or {data: [...]} envelope.
        Return the list, raise ValueError on anything else.
        """
        if isinstance(body, list):
            return body
        if isinstance(body, dict) and isinstance(body.get("data"), list):
            return body["data"]
        raise ValueError(
            f"unexpected OW response shape: expected list or {{data: [...]}}, "
            f"got {type(body).__name__}"
        )

    # OW's timeseries types use different names than our internal data_type keys.
    _OW_TIMESERIES_TYPE_MAP = {
        "heart_rate": "heart_rate",
        "heart_rate_variability": "heart_rate_variability_rmssd",
        "oxygen_saturation": "oxygen_saturation",
    }

    def fetch_timeseries(self, ow_user_id: str, types: list[str], start, end) -> list[dict]:
        """GET /api/v1/users/{id}/timeseries

        Stock OW expects repeated query params for types (not comma-separated)
        and uses its own enum names (e.g. heart_rate_variability_rmssd).
        """
        ow_types = [self._OW_TIMESERIES_TYPE_MAP.get(t, t) for t in types]
        # requests library sends list values as repeated params: types=a&types=b
        response = requests.get(
            f"{_base_url()}/api/v1/users/{ow_user_id}/timeseries",
            headers=_headers(),
            params=[("types", t) for t in ow_types]
                   + [("start_time", start.isoformat()), ("end_time", end.isoformat())],
            timeout=60,
        )
        response.raise_for_status()
        return self._unwrap_data_list(response.json())

    def fetch_summaries(self, ow_user_id: str, start, end) -> list[dict]:
        """GET /api/v1/users/{id}/summaries/activity"""
        response = requests.get(
            f"{_base_url()}/api/v1/users/{ow_user_id}/summaries/activity",
            headers=_headers(),
            params={
                "start_date": start.date().isoformat(),
                "end_date": end.date().isoformat(),
            },
            timeout=60,
        )
        response.raise_for_status()
        return self._unwrap_data_list(response.json())

    def fetch_sleep_details(self, ow_user_id: str, start, end) -> list[dict]:
        """GET /api/v1/users/{id}/events/sleep"""
        response = requests.get(
            f"{_base_url()}/api/v1/users/{ow_user_id}/events/sleep",
            headers=_headers(),
            params={
                "start_date": start.date().isoformat(),
                "end_date": end.date().isoformat(),
            },
            timeout=60,
        )
        response.raise_for_status()
        return self._unwrap_data_list(response.json())


# ---- Polling pipeline (v1) — module-level helpers ----


def load_and_validate_polling_config() -> dict:
    """Read polling-related JheSetting keys and validate them.

    Returns a dict with keys: api_base_url, api_key, lookback_days,
    initial_backfill_days, ingest_mode, webhook_secret.

    Raises ``ImproperlyConfigured`` on any failure.
    """
    cfg = {
        "api_base_url": (get_setting("ow.api_base_url") or "").strip(),
        "api_key": (get_setting("ow.api_key") or "").strip(),
        "lookback_days": int(get_setting("ow.lookback_days") or 7),
        "initial_backfill_days": int(get_setting("ow.initial_backfill_days") or 30),
        "ingest_mode": (get_setting("ow.ingest_mode") or "polling").strip(),
        "webhook_secret": (get_setting("ow.webhook_secret") or "").strip(),
    }

    parsed = urlparse(cfg["api_base_url"])
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ImproperlyConfigured("ow.api_base_url must be a valid http(s) URL")
    if not cfg["api_key"]:
        raise ImproperlyConfigured("ow.api_key must be non-empty")
    if cfg["lookback_days"] < 3:
        raise ImproperlyConfigured("ow.lookback_days must be >= 3")
    if cfg["initial_backfill_days"] < cfg["lookback_days"]:
        raise ImproperlyConfigured("ow.initial_backfill_days must be >= ow.lookback_days")
    if cfg["ingest_mode"] not in ("polling", "webhook", "disabled"):
        raise ImproperlyConfigured("ow.ingest_mode must be one of: polling, webhook, disabled")
    if cfg["ingest_mode"] == "webhook" and not cfg["webhook_secret"]:
        raise ImproperlyConfigured("ow.webhook_secret must be set when ow.ingest_mode=webhook")

    return cfg


ow_service = OWIntegrationService()
