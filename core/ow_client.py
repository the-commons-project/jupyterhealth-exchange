import logging

import requests

from core.jhe_settings.service import get_setting

logger = logging.getLogger(__name__)


def _base_url():
    return get_setting("ow.api_url", "http://localhost:8001").rstrip("/")


def _headers():
    return {"X-Open-Wearables-API-Key": get_setting("ow.api_key", "")}


def create_user(email):
    existing_id = get_user_by_email(email)
    if existing_id:
        return existing_id

    r = requests.post(f"{_base_url()}/api/v1/users", json={"email": email}, headers=_headers())
    if r.status_code == 201:
        return r.json()["id"]
    if r.status_code == 409:
        existing_id = get_user_by_email(email)
        if existing_id:
            return existing_id
        raise RuntimeError(f"OW returned 409 for email={email} but no user found on lookup")
    r.raise_for_status()
    raise RuntimeError(f"Unexpected OW response creating user: status={r.status_code}")


def get_user_by_email(email):
    r = requests.get(f"{_base_url()}/api/v1/users", params={"email": email}, headers=_headers())
    r.raise_for_status()
    data = r.json()
    items = data.get("items", data) if isinstance(data, dict) else data
    if items:
        return items[0]["id"]
    return None


def get_authorize_url(provider, ow_user_id, redirect_uri):
    r = requests.get(
        f"{_base_url()}/api/v1/oauth/{provider}/authorize",
        params={"user_id": ow_user_id, "redirect_uri": redirect_uri},
        headers=_headers(),
        allow_redirects=False,
    )
    if r.status_code == 302:
        location = r.headers.get("Location")
        if not location:
            raise RuntimeError(f"OW returned 302 for {provider} authorize without Location header")
        return location
    if r.status_code == 200:
        data = r.json()
        url = data.get("authorization_url")
        if not url:
            raise RuntimeError(f"OW returned 200 for {provider} authorize without authorization_url field")
        return url
    r.raise_for_status()
    raise RuntimeError(f"Unexpected OW response for {provider} authorize: status={r.status_code}")


def revoke_connection(ow_user_id, provider):
    r = requests.delete(f"{_base_url()}/api/v1/users/{ow_user_id}/connections/{provider}", headers=_headers())
    if r.status_code == 404:
        return
    r.raise_for_status()


def get_heart_rate_data(ow_user_id, start_time, end_time):
    r = requests.get(
        f"{_base_url()}/api/v1/users/{ow_user_id}/timeseries",
        params={"types": "heart_rate", "start_time": start_time, "end_time": end_time},
        headers=_headers(),
    )
    r.raise_for_status()
    data = r.json()
    return data.get("data", data) if isinstance(data, dict) else data
