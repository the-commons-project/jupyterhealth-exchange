"""auth.practitioner_clients.allowed_emails: only the listed practitioners' API keys can create,
get tokens, or use tokens. Empty means everyone (the default)."""

import base64

import pytest
from django.core.cache import cache
from django.core.exceptions import ValidationError
from oauth2_provider.models import get_application_model
from rest_framework.test import APIClient

from core.models import JheSetting, JheUser, PractitionerClient
from core.services.jhe_settings import validate_setting

Application = get_application_model()
KEY = "auth.practitioner_clients.allowed_emails"


@pytest.fixture(autouse=True)
def clear_cache():
    cache.clear()
    yield
    cache.clear()


def allow(value):
    setting, _ = JheSetting.objects.update_or_create(key=KEY, defaults={"value_type": "string"})
    setting.set_value("string", value)
    setting.save()
    cache.clear()


def practitioner(email):
    return JheUser.objects.create_user(email=email, password="testpass123", user_type="practitioner")


def make_key(user):
    api = APIClient()
    api.force_authenticate(user)
    r = api.post("/api/v1/practitioner_clients", {"label": "k"}, format="json")
    assert r.status_code == 201, r.text
    return PractitionerClient.objects.get(id=r.json()["id"]).application


def request_token(application):
    credentials = base64.b64encode(f"{application.client_id}:{application.client_secret}".encode()).decode()
    return APIClient().post(
        "/o/token/", {"grant_type": "client_credentials"}, HTTP_AUTHORIZATION=f"Basic {credentials}"
    )


def call_api(token):
    return APIClient().get("/api/v1/users/profile", HTTP_AUTHORIZATION=f"Bearer {token}")


@pytest.mark.django_db
def test_everyone_may_use_keys_when_the_setting_is_empty():
    app = make_key(practitioner("a@example.org"))
    r = request_token(app)
    assert r.status_code == 200
    assert call_api(r.json()["access_token"]).status_code == 200


@pytest.mark.django_db
def test_only_listed_owners_get_tokens():
    svc, other = practitioner("svc@example.org"), practitioner("other@example.org")
    svc_app, other_app = make_key(svc), make_key(other)
    allow("svc@example.org")
    assert request_token(svc_app).status_code == 200
    assert request_token(other_app).status_code == 401


@pytest.mark.django_db
def test_tokens_issued_before_the_allowlist_stop_working():
    svc, other = practitioner("svc@example.org"), practitioner("other@example.org")
    svc_token = request_token(make_key(svc)).json()["access_token"]
    other_token = request_token(make_key(other)).json()["access_token"]
    assert call_api(other_token).status_code == 200
    allow("svc@example.org")
    assert call_api(svc_token).status_code == 200
    assert call_api(other_token).status_code in (401, 403)


@pytest.mark.django_db
def test_only_listed_owners_can_create_keys():
    svc, other = practitioner("svc@example.org"), practitioner("other@example.org")
    allow("svc@example.org")
    api = APIClient()
    api.force_authenticate(other)
    assert api.post("/api/v1/practitioner_clients", {"label": "x"}, format="json").status_code == 403
    api.force_authenticate(svc)
    assert api.post("/api/v1/practitioner_clients", {"label": "x"}, format="json").status_code == 201


@pytest.mark.django_db
def test_match_ignores_case_and_accepts_commas_and_spaces():
    app = make_key(practitioner("Svc@Example.org"))
    allow("other@example.org, svc@example.ORG")
    assert request_token(app).status_code == 200


@pytest.mark.django_db
def test_other_oauth_applications_are_unaffected():
    from core.auth import application_allowed

    allow("svc@example.org")
    plain = Application.objects.create(
        name="plain",
        client_type=Application.CLIENT_CONFIDENTIAL,
        authorization_grant_type=Application.GRANT_CLIENT_CREDENTIALS,
    )
    assert application_allowed(plain) is True
    assert application_allowed(None) is True


def test_setting_is_validated():
    validate_setting(KEY, "string", "")
    validate_setting(KEY, "string", "a@example.org, b@example.org")
    for bad in ("not-an-email", "a@example.org b", "@@"):
        with pytest.raises(ValidationError):
            validate_setting(KEY, "string", bad)
