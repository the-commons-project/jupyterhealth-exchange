"""SSO-only sign-in for practitioners/staff (auth.sso.required_for_practitioners) and the
network allowlist for /admin/ (auth.admin.allowed_cidrs)."""

import ipaddress

import pytest
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.test import RequestFactory
from django.urls import reverse

from core.middleware import client_ip
from core.models import JheSetting, JheUser
from core.services.jhe_settings import validate_setting


def set_setting(key, value_type, value):
    setting, _ = JheSetting.objects.update_or_create(key=key, defaults={"value_type": value_type})
    setting.set_value(value_type, value)
    setting.save()
    cache.clear()


@pytest.fixture(autouse=True)
def clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def sso_only(db):
    """SSO-only switched on, with a working SAML application (the switch needs one to take effect)."""
    from allauth.socialaccount.models import SocialApp

    SocialApp.objects.create(
        provider="saml",
        provider_id="uchicago",
        name="UChicago",
        client_id="uchicago",
        settings={"idp": {"entity_id": "x", "sso_url": "https://idp.example.org/sso", "x509cert": "x"}},
    )
    set_setting("auth.sso.saml2", "int", 1)
    set_setting("auth.sso.required_for_practitioners", "bool", True)


def make(email, **kw):
    return JheUser.objects.create_user(email=email, password="testpass123", **kw)


def login(client, email):
    return client.post(reverse("login"), {"username": email, "password": "testpass123"})


@pytest.mark.parametrize(
    "kw",
    [
        {"user_type": "practitioner"},
        {"user_type": "practitioner", "is_staff": True},
        {"user_type": "practitioner", "is_staff": True, "is_superuser": True},
    ],
)
def test_password_login_closed_when_sso_only(client, sso_only, kw):
    make("p@example.org", **kw)
    response = login(client, "p@example.org")
    assert response.status_code == 403
    assert "_auth_user_id" not in client.session


@pytest.mark.django_db
def test_password_login_works_when_not_enforced(client):
    make("p@example.org", user_type="practitioner")
    response = login(client, "p@example.org")
    assert response.status_code == 302 and "_auth_user_id" in client.session


@pytest.mark.django_db
def test_setting_without_a_working_saml_app_does_not_lock_everyone_out(client):
    make("p@example.org", user_type="practitioner")
    set_setting("auth.sso.required_for_practitioners", "bool", True)  # no SocialApp, saml2 switch off
    assert login(client, "p@example.org").status_code == 302
    assert b'<input name="username"' in client.get(reverse("login")).content


def test_login_page_shows_only_the_saml_button_when_sso_only(client, sso_only):
    page = client.get(reverse("login")).content
    assert b"Continue with SAML2" in page
    assert b'<input name="username"' not in page and b"Sign Up" not in page and b"Forgot Password" not in page


def test_home_page_has_no_signup_button_when_sso_only(client, sso_only):
    page = client.get("/").content
    assert b"Log In" in page and b"Sign Up" not in page


def test_home_page_shows_signup_button_when_not_enforced(client, db):
    assert b"Sign Up" in client.get("/").content


def test_signup_closed_when_sso_only(client, sso_only):
    before = JheUser.objects.count()
    for method in (client.get, client.post):
        assert method(reverse("signup")).status_code == 403
    assert JheUser.objects.count() == before


def test_signup_page_open_when_not_enforced(client, db):
    assert client.get(reverse("signup")).status_code == 200


def _admin_get(client, ip, xff=None):
    extra = {"REMOTE_ADDR": "10.0.0.9"}
    if xff is not None:
        extra["HTTP_X_FORWARDED_FOR"] = xff
    return client.get("/admin/login/", **extra)


@pytest.mark.django_db
def test_admin_open_when_allowlist_empty(client):
    assert _admin_get(client, None, "8.8.8.8").status_code == 200


@pytest.mark.django_db
def test_admin_allowlist_admits_listed_networks_only(client):
    set_setting("auth.admin.allowed_cidrs", "string", "203.0.113.0/24, 198.51.100.7")
    assert _admin_get(client, None, "203.0.113.50").status_code == 200
    assert _admin_get(client, None, "198.51.100.7").status_code == 200
    assert _admin_get(client, None, "8.8.8.8").status_code == 403


@pytest.mark.django_db
def test_admin_allowlist_ignores_forged_forwarded_entries(client):
    set_setting("auth.admin.allowed_cidrs", "string", "203.0.113.0/24")
    # The client claims a listed address, but the load balancer appended the real one last.
    assert _admin_get(client, None, "203.0.113.5, 8.8.8.8").status_code == 403
    assert _admin_get(client, None, "8.8.8.8, 203.0.113.5").status_code == 200


@pytest.mark.django_db
def test_allowlist_covers_the_settings_api_too(client):
    set_setting("auth.admin.allowed_cidrs", "string", "203.0.113.0/24")
    assert client.get("/api/v1/jhe_settings", HTTP_X_FORWARDED_FOR="8.8.8.8").status_code == 403
    assert client.get("/api/v1/jhe_settings/1", HTTP_X_FORWARDED_FOR="8.8.8.8").status_code == 403
    assert client.get("/api/v1/jhe_settings", HTTP_X_FORWARDED_FOR="203.0.113.5").status_code != 403


@pytest.mark.django_db
def test_allowlist_covers_only_admin(client):
    set_setting("auth.admin.allowed_cidrs", "string", "203.0.113.0/24")
    assert client.get("/health", REMOTE_ADDR="8.8.8.8").status_code == 200
    assert client.get(reverse("login"), HTTP_X_FORWARDED_FOR="8.8.8.8").status_code == 200


def test_client_ip_without_header_is_the_peer():
    request = RequestFactory().get("/", REMOTE_ADDR="192.0.2.1")
    assert client_ip(request) == ipaddress.ip_address("192.0.2.1")
    assert client_ip(RequestFactory().get("/", REMOTE_ADDR="nonsense")) is None


def test_cidr_setting_is_validated():
    validate_setting("auth.admin.allowed_cidrs", "string", "")
    validate_setting("auth.admin.allowed_cidrs", "string", "10.0.0.0/8 2001:db8::/32")
    with pytest.raises(ValidationError):
        validate_setting("auth.admin.allowed_cidrs", "string", "10.0.0.0/8, nonsense")


@pytest.mark.django_db
def test_otp_login_refused_for_practitioners_when_sso_only(client, sso_only):
    from allauth.account.adapter import get_adapter
    from django.contrib.messages.storage.fallback import FallbackStorage

    user = make("p@example.org", user_type="practitioner")
    patient = make("pt@example.org", user_type="patient")
    request = RequestFactory().get("/")
    request.session = {}
    request._messages = FallbackStorage(request)
    kw = dict(email_verification="none", email=None, signup=False, redirect_url=None)
    assert get_adapter().pre_login(request, user, signal_kwargs=None, **kw).status_code == 302
    assert get_adapter().pre_login(request, user, signal_kwargs={"sociallogin": object()}, **kw) is None
    assert get_adapter().pre_login(request, patient, signal_kwargs=None, **kw) is None


@pytest.mark.parametrize(
    "path",
    ["/allauth/signup/", "/allauth/password/reset/", "/accounts/password_reset/", "/accounts/reset/x/y/"],
)
def test_signup_and_password_reset_routes_closed_when_sso_only(client, sso_only, path):
    assert client.get(path).status_code == 403


@pytest.mark.django_db
def test_allauth_signup_cannot_create_accounts_when_sso_only(client, sso_only):
    before = JheUser.objects.count()
    client.post("/allauth/signup/", {"email": "new@example.org", "password1": "Zx9!kqpLm2", "password2": "Zx9!kqpLm2"})
    assert JheUser.objects.count() == before


@pytest.mark.django_db
@pytest.mark.parametrize("path", ["/allauth/signup/", "/allauth/password/reset/", "/accounts/password_reset/"])
def test_signup_and_password_reset_routes_open_when_not_enforced(client, path):
    assert client.get(path).status_code == 200
