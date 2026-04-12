"""Tests for the OW proxy views: list_providers and provider_callback_proxy."""

from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import pytest
from rest_framework.test import APIClient

from core.models import JheSetting
from core.views.ow import _PROVIDER_KEY_RE


# ---------------------------------------------------------------------------
# Provider key regex
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    ["oura", "garmin", "whoop", "polar", "ultra-human", "ouraring2", "a", "ABC"[:0] or "x"],
)
def test_provider_regex_accepts_valid(value):
    assert _PROVIDER_KEY_RE.fullmatch(value) is not None


@pytest.mark.parametrize(
    "value",
    [
        "../etc/passwd",
        "oura/../evil",
        "oura?evil=1",
        "oura&garmin",
        "oura ",
        " oura",
        "OURA",          # uppercase rejected
        "oura.com",      # dot rejected
        "x" * 33,        # too long
        "",              # empty
        "%2e%2e",        # url-encoded escape
        "oura\nmore",
    ],
)
def test_provider_regex_rejects_invalid(value):
    assert _PROVIDER_KEY_RE.fullmatch(value) is None


# ---------------------------------------------------------------------------
# provider_callback_proxy
# ---------------------------------------------------------------------------


@pytest.fixture
def ow_configured(db):
    from django.core.cache import cache
    JheSetting.objects.update_or_create(
        key="ow.api_base_url",
        defaults={"value_type": "string", "value_string": "http://ow.test"},
    )
    JheSetting.objects.update_or_create(
        key="ow.api_key",
        defaults={"value_type": "string", "value_string": "test-key"},
    )
    # Clear LocMem cache so the new values are picked up by get_setting()
    cache.clear()
    yield
    cache.clear()


def test_callback_proxy_rejects_invalid_provider(ow_configured):
    client = APIClient()
    resp = client.get("/api/v1/oauth/..%2Fevil/callback?code=abc")
    # Either Django's URL routing rejects it or our regex does — either way
    # we should never see a 302 to an attacker-controlled URL.
    assert resp.status_code in (400, 404)


def test_callback_proxy_redirects_to_ow_for_valid_provider(ow_configured):
    client = APIClient()
    resp = client.get("/api/v1/oauth/oura/callback?code=abc&state=xyz")
    assert resp.status_code == 302
    location = resp["Location"]
    assert location.startswith("http://ow.test/api/v1/oauth/oura/callback")
    # Query params preserved via urlencode
    qs = parse_qs(urlparse(location).query)
    assert qs.get("code") == ["abc"]
    assert qs.get("state") == ["xyz"]
    # Cache-Control: no-store on responses containing OAuth codes
    assert resp["Cache-Control"] == "no-store"


def test_callback_proxy_returns_503_when_ow_not_configured(db):
    # JheSetting values are cached in LocMem and persist across tests in the
    # same process — clear both the DB rows and the cache.
    from django.core.cache import cache
    JheSetting.objects.filter(key__startswith="ow.").delete()
    cache.clear()
    client = APIClient()
    resp = client.get("/api/v1/oauth/oura/callback?code=abc")
    assert resp.status_code == 503
    body = resp.json()
    # Generic error message — no internal config key names
    assert "ow.api_base_url" not in str(body).lower()


# ---------------------------------------------------------------------------
# list_providers
# ---------------------------------------------------------------------------


def test_list_providers_requires_authentication(ow_configured):
    client = APIClient()
    resp = client.get("/api/v1/ow/providers")
    assert resp.status_code in (401, 403)


@patch("core.views.ow.ow_service")
def test_list_providers_rewrites_relative_icon_urls(mock_ow, ow_configured, api_client):
    mock_ow.list_providers.return_value = [
        {"provider": "oura", "name": "Oura", "icon_url": "/static/icons/oura.svg", "is_enabled": True},
        {"provider": "garmin", "name": "Garmin", "icon_url": "https://garmin.example.org/icon.svg", "is_enabled": True},
    ]
    resp = api_client.get("/api/v1/ow/providers")
    assert resp.status_code == 200
    body = resp.json()
    assert body[0]["iconUrl"].startswith("http://ow.test/")
    # Absolute URLs are left alone
    assert body[1]["iconUrl"] == "https://garmin.example.org/icon.svg" or \
           body[1]["icon_url"] == "https://garmin.example.org/icon.svg"


@patch("core.views.ow.ow_service")
def test_list_providers_handles_unexpected_shape(mock_ow, ow_configured, api_client):
    mock_ow.list_providers.return_value = "not a list"
    resp = api_client.get("/api/v1/ow/providers")
    assert resp.status_code == 502


@patch("core.views.ow.ow_service")
def test_list_providers_handles_ow_failure(mock_ow, ow_configured, api_client):
    import requests
    mock_ow.list_providers.side_effect = requests.RequestException("connection refused")
    resp = api_client.get("/api/v1/ow/providers")
    assert resp.status_code == 502
    body = resp.json()
    # Generic message — no stack trace, no internal details
    assert "connection refused" not in str(body)
