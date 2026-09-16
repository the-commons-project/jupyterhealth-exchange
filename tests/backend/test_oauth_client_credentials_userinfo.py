"""Regression coverage for jupyterhealth/jupyterhealth-exchange#742.

A client-credentials token (minted via "Generate Bearer Token" /
PractitionerClient) has no resource owner -- django-oauth-toolkit saves the
AccessToken with user=None. JheOAuth2Validator.validate_bearer_token falls
back to the owning Application's user for every bearer-token consumer, not
just DRF views, so both /o/userinfo/ and JHE's own API resolve request.user
the same way. These tests exercise real tokens over HTTP (not
force_authenticate), because that fallback only runs on the real token-
validation path.
"""

import base64

from oauth2_provider.models import get_application_model
from rest_framework.test import APIClient

from core.models import PractitionerClient

Application = get_application_model()


def _issue_client_credentials_token(api_client, application):
    credentials = base64.b64encode(f"{application.client_id}:{application.client_secret}".encode()).decode()
    r = api_client.post(
        "/o/token/",
        {"grant_type": "client_credentials"},
        HTTP_AUTHORIZATION=f"Basic {credentials}",
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _practitioner_client_application(user, label="test client"):
    api_client = APIClient()
    api_client.force_authenticate(user)
    r = api_client.post("/api/v1/practitioner_clients", {"label": label}, format="json")
    assert r.status_code == 201, r.text
    pc = PractitionerClient.objects.get(id=r.json()["id"])
    return pc.application


def test_client_credentials_token_resolves_userinfo(user):
    application = _practitioner_client_application(user)
    api_client = APIClient()
    token = _issue_client_credentials_token(api_client, application)

    r = api_client.get("/o/userinfo/", HTTP_AUTHORIZATION=f"Bearer {token}")

    assert r.status_code == 200, r.text
    assert r.json()["sub"] == str(user.pk)


def test_client_credentials_token_resolves_drf_profile(user):
    application = _practitioner_client_application(user)
    api_client = APIClient()
    token = _issue_client_credentials_token(api_client, application)

    r = api_client.get("/api/v1/users/profile", HTTP_AUTHORIZATION=f"Bearer {token}")

    assert r.status_code == 200, r.text
    assert r.json()["email"] == user.email
