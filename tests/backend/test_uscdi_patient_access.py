"""USCDI Patient Access in JHE: the page, the launch and its callback cookie, the callback's order
of checks, the job endpoints, sign-out, the settings keys, and one pull end to end into a (moto)
bucket."""

import base64
import hashlib
import io
import json
import logging
import secrets
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import boto3
import pytest
import responses
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.forms.models import model_to_dict
from django.test import Client, override_settings
from django.test.utils import override_script_prefix
from django.utils import timezone
from moto import mock_aws
from oauth2_provider.models import AccessToken, RefreshToken, get_application_model
from rest_framework.test import APIClient

from core.models import (
    ClientDataSource,
    CodeableConcept,
    DataSource,
    DataSourceSupportedScope,
    EhrBrand,
    EhrBrandLocation,
    EhrVendor,
    FhirSource,
    JheClient,
    JheSetting,
    JheUser,
    PatientIdentifier,
    PatientInvitation,
    StudyDataSource,
    StudyPatientScopeConsent,
    StudyScopeRequest,
)
from core.patient_access.consent import require_consent
from core.patient_access.directory import JheOrmDirectory
from core.patient_access.settings import read_patient_access_settings
from core.views.uscdi_patient_access import CALLBACK_COOKIE_PREFIX, CALLBACK_COOKIE_SECONDS, Reason
from patient_access.core.flow import FlowErrorKind
from patient_access.core.models import JobStatus
from patient_access.core.pipeline import DeliveryRefused
from patient_access.core.settings import DEFAULT_LAYOUT
from patient_access.core.smart import clear_discovery_cache
from patient_access.django_app.models import AuthAttemptRecord, JobRecord
from patient_access.django_app.wiring import get_module

from .utils import add_patient_to_study, create_study

ISS = "https://ehr.example.org/fhir"
AUTHORIZE = "https://ehr.example.org/authorize"
TOKEN = "https://ehr.example.org/token"
CONNECT = "/clients/uscdi-patient-access/"
CALLBACK = "/clients/uscdi-patient-access/callback"
LAUNCH = "/api/v1/uscdi-patient-access/launch"
SIGN_OUT = "/api/v1/uscdi-patient-access/sign-out"
TEST_KEY = "x9Q5mX1y8m0bkYb3l7wVvO9Oe2r3wQ4b8h9vB9fP0nU="
BUCKET = "pulls"


@pytest.fixture(autouse=True)
def fresh_settings_cache():
    """get_setting caches per process; a rolled-back row from another test must not linger."""
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def module_on(tmp_path):
    config = {
        "ENCRYPTION_KEYS": [TEST_KEY],
        "SPOOL_DIR": str(tmp_path / "spool"),
        "DISPATCHER": "none",  # tests run the job themselves, in-process
        "LAUNCH_ATTRS": "core.patient_access.identity.LaunchAttrs",
        "JOB_ATTRS": "core.patient_access.identity.JheAttrs",
        "DIRECTORY": "core.patient_access.directory.JheOrmDirectory",
        "SETTINGS_PROVIDER": "core.patient_access.settings.read_patient_access_settings",
        "DELIVERY_GUARD": "core.patient_access.consent.require_consent",
    }
    with override_settings(PATIENT_ACCESS=config):
        yield get_module()


@pytest.fixture
def world(db, organization, patient):
    """The client, its data source with the wildcard scope, a study the patient consented to, and
    one EHR facility on a vendor the registry knows."""
    star, _ = CodeableConcept.objects.get_or_create(
        coding_system="http://hl7.org/fhir/resource-types", coding_code="*", defaults={"text": "All FHIR Resources"}
    )
    ds = DataSource.objects.create(name="USCDI Patient Access", type="patient_app")
    DataSourceSupportedScope.objects.create(data_source=ds, scope_code=star)
    app = get_application_model().objects.create(
        name="USCDI Patient Access",
        client_type="public",
        authorization_grant_type="authorization-code",
        redirect_uris="http://testserver/auth/callback",
        algorithm="RS256",  # as the seed makes it: /o/token/ signs an id_token
    )
    JheClient.objects.create(
        application=app, invitation_url="http://testserver/clients/uscdi-patient-access/?code=CODE"
    )
    ClientDataSource.objects.create(client=app, data_source=ds)
    study = create_study(organization=organization)
    StudyScopeRequest.objects.create(study=study, scope_code=star)
    StudyDataSource.objects.create(study=study, data_source=ds)
    add_patient_to_study(patient, study)
    vendor = EhrVendor.objects.create(
        name="SMART Sandbox", ehr_client_id="client-1", supported_scopes="patient/Patient.read patient/Observation.read"
    )
    brand = EhrBrand.objects.create(name="Example Health", vendor=vendor, fhir_base_url=ISS)
    location = EhrBrandLocation.objects.create(brand=brand, name="Example Clinic", address_text="1 Main St")
    return {"app": app, "ds": ds, "study": study, "location": location, "patient": patient}


@pytest.fixture
def ehr():
    clear_discovery_cache()
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.add(
            responses.GET,
            f"{ISS}/.well-known/smart-configuration",
            json={"authorization_endpoint": AUTHORIZE, "token_endpoint": TOKEN},
        )
        yield rsps
    clear_discovery_cache()


def bearer(user, app) -> dict[str, str]:
    """A real OAuth access token for `user`, as the shell holds after redeeming the link."""
    token = AccessToken.objects.create(
        user=user,
        application=app,
        token=f"token-{secrets.token_hex(8)}",
        expires=timezone.now() + timedelta(hours=1),
        scope="openid",
    )
    return {"HTTP_AUTHORIZATION": f"Bearer {token.token}"}


def patient_browser(world) -> Client:
    """The patient's browser: the bearer token on every request, and Django's CSRF checks on, as
    in production. Its cookie jar keeps what the launch sets."""
    return Client(enforce_csrf_checks=True, **bearer(world["patient"].jhe_user, world["app"]))


def launch_body(world, scopes=("patient/Observation.read",), **fields) -> dict:
    return {
        "location_id": str(world["location"].id),
        "data_source_id": world["ds"].id,
        "scopes": list(scopes),
        **fields,
    }


def launch(browser: Client, world, scopes=("patient/Observation.read",), **fields):
    """The connect step's launch, as the client sends it: JSON with the bearer token."""
    return browser.post(LAUNCH, launch_body(world, scopes, **fields), content_type="application/json")


def authorize_url(r) -> str:
    assert r.status_code == 200, r.content
    url = r.json()["authorizeUrl"]
    assert url.startswith(AUTHORIZE), url
    return url


def state_of(url: str) -> str:
    return parse_qs(urlparse(url).query)["state"][0]


def cookie_name(state: str) -> str:
    return f"{CALLBACK_COOKIE_PREFIX}{hashlib.sha256(state.encode()).hexdigest()[:16]}"


def launched(world, browser: Client | None = None):
    browser = browser or patient_browser(world)
    return browser, state_of(authorize_url(launch(browser, world)))


def token_response(ehr, patient="ehr-p1"):
    ehr.add(
        responses.POST,
        TOKEN,
        json={
            "access_token": "ehr-token",
            "token_type": "Bearer",
            "patient": patient,
            "scope": "openid patient/Observation.read",
        },
    )


def refused(r) -> str:
    """A refusal's redirect: the error route with a reason from the closed set, and nothing else
    in the URL."""
    assert r.status_code == 302, r.content
    url = urlparse(r["Location"])
    query = parse_qs(url.query)
    assert url.path == CONNECT and set(query) == {"route", "reason"} and query["route"] == ["error"], r["Location"]
    return Reason(query["reason"][0]).value


def token_endpoint_called(ehr) -> bool:
    return any(c.request.method == "POST" for c in ehr.calls)


# --- the page and the invitation -------------------------------------------------------------


def test_the_page_serves_the_shell_and_redeems_nothing_itself(world, module_on):
    """The shell redeems ?code= at JHE's invitation endpoint, as for the EHR Patient Portal."""
    invitation = PatientInvitation.issue(world["patient"], world["app"])
    r = Client().get(CONNECT, {"code": f"testserver_{invitation.token}"})
    assert r.status_code == 200
    assert PatientInvitation.objects.get(id=invitation.id).status == PatientInvitation.Status.ISSUED
    config = r.context["config"]
    assert config["available"] is True and config["allowHttpSignIn"] is False
    assert config["typeChoiceByVendor"]["Epic Sandbox"] == "ehr"
    assert config["typeChoiceByVendor"]["SMART Sandbox"] == "host"
    assert not r.cookies  # no Django session, no CSRF cookie


def test_the_page_says_when_the_module_is_off(world):
    assert Client().get(CONNECT).context["config"]["available"] is False


@pytest.fixture
def oidc_signing_key(settings):
    """A real key for /o/token/ to sign this RS256 app's id_token with: CI's OIDC_RSA_PRIVATE_KEY
    is a placeholder that does not parse."""
    pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    settings.OAUTH2_PROVIDER = {**settings.OAUTH2_PROVIDER, "OIDC_RSA_PRIVATE_KEY": pem.decode()}


def test_this_clients_invitation_redeems_for_a_token_that_launches_and_signs_out(
    world, module_on, ehr, oidc_signing_key
):
    """The whole token path, as the shell walks it: the invitation for a grant at the generic
    endpoint, the grant for tokens at /o/token/, a launch with the access token, then sign-out,
    which revokes the access token and the refresh token issued with it."""
    invitation = PatientInvitation.issue(world["patient"], world["app"])
    r = Client().post(f"/api/v1/invitation/{invitation.token}")
    assert r.status_code == 200, r.content
    grant = r.json()["grant"]
    assert grant["client_id"] == world["app"].client_id
    world["app"].redirect_uris = grant["redirect_uri"]
    world["app"].save()
    verifier = base64.urlsafe_b64encode(invitation.token.encode()).rstrip(b"=").decode()
    tokens = Client().post(
        "/o/token/",
        {
            "grant_type": "authorization_code",
            "code": grant["code"],
            "redirect_uri": grant["redirect_uri"],
            "client_id": grant["client_id"],
            "code_verifier": verifier,
        },
    )
    assert tokens.status_code == 200, tokens.content
    access, refresh = tokens.json()["access_token"], tokens.json()["refresh_token"]
    browser = Client(enforce_csrf_checks=True, HTTP_AUTHORIZATION=f"Bearer {access}")
    authorize_url(launch(browser, world))
    assert browser.post(SIGN_OUT).status_code == 204
    assert browser.get("/api/v1/users/profile").status_code == 401
    assert RefreshToken.objects.get(token_checksum=hashlib.sha256(refresh.encode()).hexdigest()).revoked is not None


def test_the_reuse_window_runs_from_first_use_however_often_the_link_is_opened(world):
    invitation = PatientInvitation.issue(world["patient"], world["app"])
    redeem = f"/api/v1/invitation/{invitation.token}"
    assert Client().post(redeem).status_code == 200
    PatientInvitation.objects.filter(id=invitation.id).update(last_updated=timezone.now() - timedelta(hours=11))
    first_use = PatientInvitation.objects.get().last_updated
    assert Client().post(redeem).status_code == 200  # inside the window
    assert PatientInvitation.objects.get().last_updated == first_use  # and the window did not restart
    PatientInvitation.objects.filter(id=invitation.id).update(last_updated=timezone.now() - timedelta(hours=13))
    assert Client().post(redeem).status_code == 409


# --- launch ----------------------------------------------------------------------------------


def test_launch_answers_with_the_authorize_url_and_records_the_attempt(world, module_on, ehr):
    r = launch(patient_browser(world), world)
    q = parse_qs(urlparse(authorize_url(r)).query)
    assert q["client_id"] == ["client-1"]
    assert q["redirect_uri"] == ["http://testserver/clients/uscdi-patient-access/callback"]
    assert r["Cache-Control"] == "no-store"
    attempt = AuthAttemptRecord.objects.get()
    assert attempt.participant_ref == str(world["patient"].id)
    assert attempt.body["scopes_declined"] == ["patient/Patient.read"]


def test_launch_sets_one_callback_cookie_for_the_attempt(world, module_on, ehr):
    r = launch(patient_browser(world), world)
    state = state_of(authorize_url(r))
    assert list(r.cookies) == [cookie_name(state)]
    cookie = r.cookies[cookie_name(state)]
    assert cookie["httponly"] and cookie["samesite"] == "Lax"
    assert cookie["path"] == CALLBACK  # the redirect URI's path: sent to the callback alone
    assert cookie["max-age"] == CALLBACK_COOKIE_SECONDS > 600  # outlives the ten-minute attempt
    assert cookie["secure"]  # DEBUG is off
    row = AuthAttemptRecord.objects.get()
    stored = json.dumps(model_to_dict(row), default=str)
    assert cookie.value not in stored  # the attempt keeps the hash, never the value
    assert (
        row.body["participant"]["attrs"]["callback_cookie_sha256"] == hashlib.sha256(cookie.value.encode()).hexdigest()
    )


@pytest.mark.parametrize(
    ("debug", "https", "secure"),
    [(True, False, False), (True, True, True), (False, False, True), (False, True, True)],
    ids=["development over http", "development over https", "DEBUG off over http", "DEBUG off over https"],
)
def test_the_callback_cookie_is_secure_unless_development_over_http(world, module_on, ehr, debug, https, secure):
    with override_settings(DEBUG=debug):
        r = patient_browser(world).post(LAUNCH, launch_body(world), content_type="application/json", secure=https)
    (cookie,) = r.cookies.values()
    assert bool(cookie["secure"]) is secure


def test_under_a_script_prefix_the_cookie_and_the_redirect_uri_agree(world, module_on, ehr):
    """JHE mounted below the root: the cookie's path comes from the redirect URI actually sent."""
    with override_script_prefix("/jhe/"):
        r = launch(patient_browser(world), world)
    redirect_uri = parse_qs(urlparse(authorize_url(r)).query)["redirect_uri"][0]
    (cookie,) = r.cookies.values()
    assert urlparse(redirect_uri).path == cookie["path"] == "/jhe" + CALLBACK


def test_launch_requests_every_type_when_the_vendors_own_screen_asks(world, module_on, ehr):
    """Epic lists every registered category on its screen whatever we request, so the checklist
    is skipped for it and a subset sent anyway is ignored: nothing is declined on our side."""
    epic = EhrVendor.objects.create(
        name="Epic Sandbox", ehr_client_id="epic-1", supported_scopes="patient/Patient.read patient/Observation.read"
    )
    EhrBrand.objects.filter(id=world["location"].brand_id).update(vendor=epic)
    authorize_url(launch(patient_browser(world), world, scopes=("patient/Observation.read",)))
    attempt = AuthAttemptRecord.objects.get()
    assert set(attempt.body["scopes"]) >= {"patient/Patient.read", "patient/Observation.read"}
    assert attempt.body["scopes_declined"] == []


def test_launch_takes_a_token_and_nothing_else(world, module_on, ehr, user):
    assert launch(Client(), world).status_code == 401
    logged_in = Client()
    logged_in.force_login(world["patient"].jhe_user)
    assert launch(logged_in, world).status_code == 401  # a Django login is not a token
    practitioner = Client(**bearer(user, world["app"]))
    r = launch(practitioner, world)
    assert r.status_code == 403 and r.json() == {"reason": "not_a_patient"}
    assert not AuthAttemptRecord.objects.exists()


def test_launch_is_a_post(world, module_on, ehr):
    assert patient_browser(world).get(LAUNCH).status_code == 405


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda world: StudyPatientScopeConsent.objects.update(consented=False), "consent_required"),
        (
            lambda world: {"data_source_id": DataSource.objects.create(name="Other", type="patient_app").id},
            "source_unavailable",
        ),
        (lambda world: {"data_source_id": "not-a-number"}, "invalid_request"),
        (lambda world: {"location_id": "999999"}, "unknown_selection"),
        (lambda world: {"scopes": []}, "no_types"),
        (lambda world: EhrVendor.objects.update(ehr_client_id=""), "not_configured"),
    ],
    ids=[
        "no consent",
        "another client's data source",
        "malformed",
        "unknown facility",
        "nothing ticked",
        "no client id",
    ],
)
def test_a_refused_launch_answers_with_a_reason_and_records_nothing(world, module_on, ehr, change, reason):
    fields = change(world)
    r = launch(patient_browser(world), world, **(fields if isinstance(fields, dict) else {}))
    assert r.status_code == 400 and r.json() == {"reason": reason}
    assert r["Cache-Control"] == "no-store"
    assert not AuthAttemptRecord.objects.exists() and not r.cookies


def test_the_module_off_refuses_the_launch(world, ehr):
    r = launch(patient_browser(world), world)
    assert r.status_code == 503 and r.json() == {"reason": "not_available"}


def test_every_library_error_has_a_reason():
    assert all(Reason(kind.value) for kind in FlowErrorKind)


# --- callback --------------------------------------------------------------------------------


def test_the_callback_creates_the_pull_and_its_records_and_clears_the_cookie(world, module_on, ehr):
    browser, state = launched(world)
    token_response(ehr)
    r = browser.get(CALLBACK, {"state": state, "code": "c1"})
    assert r.status_code == 302 and "route=status" in r["Location"]
    assert "no-store" in r["Cache-Control"]
    job = JobRecord.objects.get()
    source = FhirSource.objects.get()
    assert source.ehr_brand_location == world["location"] and source.label == "SMART Sandbox - Example Health"
    assert job.body["participant"]["attrs"]["fhir_source_id"] == source.id
    assert job.body["participant"]["attrs"]["study_ids"] == [world["study"].id]
    assert PatientIdentifier.objects.filter(system=ISS, value="ehr-p1", patient=world["patient"]).exists()
    assert "ehr-token" not in job.credentials_enc
    cleared = r.cookies[cookie_name(state)]
    assert cleared.value == "" and cleared["max-age"] == 0 and cleared["path"] == CALLBACK


def test_a_second_pull_reuses_the_fhir_source(world, module_on, ehr):
    token_response(ehr)
    for _ in range(2):
        browser, state = launched(world)
        browser.get(CALLBACK, {"state": state, "code": "c"})
    assert FhirSource.objects.count() == 1 and JobRecord.objects.count() == 2


def test_a_browser_without_the_callback_cookie_is_refused_and_the_code_never_spent(world, module_on, ehr):
    """Someone lured into finishing another participant's sign-in: their browser never launched,
    so it holds no cookie for the attempt. Nothing reaches the EHR's token endpoint, and nothing
    is recorded: no job, no FhirSource, no EHR identifier tied to the wrong patient."""
    _, state = launched(world)
    token_response(ehr)
    r = Client().get(CALLBACK, {"state": state, "code": "victims-code"})
    assert refused(r) == "browser_mismatch"
    assert not token_endpoint_called(ehr)
    assert not JobRecord.objects.exists() and not FhirSource.objects.exists()
    assert not PatientIdentifier.objects.exists()
    assert AuthAttemptRecord.objects.get().used_at is not None  # spent: the real patient starts again
    assert r.cookies[cookie_name(state)]["max-age"] == 0


def test_another_attempts_cookie_and_a_wrong_value_are_refused(world, module_on, ehr, organization):
    """Patients A and B both launch. B's browser holds a cookie, but for B's attempt; and a cookie
    with A's name carrying B's value is not A's either."""
    token_response(ehr)
    _, state_a = launched(world)
    other = JheUser.objects.create_user(email="other@example.org", password="x", user_type="patient")
    other.patient.organizations.add(organization)
    add_patient_to_study(other.patient, world["study"])
    browser_b, state_b = launched(dict(world, patient=other.patient))
    assert refused(browser_b.get(CALLBACK, {"state": state_a, "code": "c"})) == "browser_mismatch"
    _, state_a2 = launched(world)
    browser_b.cookies[cookie_name(state_a2)] = browser_b.cookies[cookie_name(state_b)].value
    assert refused(browser_b.get(CALLBACK, {"state": state_a2, "code": "c"})) == "browser_mismatch"
    assert not token_endpoint_called(ehr) and not JobRecord.objects.exists()


def test_two_launches_in_one_browser_both_complete(world, module_on, ehr):
    """Each attempt has its own cookie, so a second launch (another tab, Back and connect again)
    leaves the first's in place."""
    token_response(ehr)
    browser, first = launched(world)
    _, second = launched(world, browser)
    for state in (second, first):
        assert "route=status" in browser.get(CALLBACK, {"state": state, "code": "c"})["Location"]
    assert JobRecord.objects.count() == 2


def test_a_replayed_callback_is_refused_even_with_the_cookie(world, module_on, ehr):
    browser, state = launched(world)
    token_response(ehr)
    value = browser.cookies[cookie_name(state)].value
    browser.get(CALLBACK, {"state": state, "code": "c"})
    browser.cookies[cookie_name(state)] = value  # re-sent by hand
    r = browser.get(CALLBACK, {"state": state, "code": "c"})
    assert refused(r) == "attempt_invalid" and r.cookies[cookie_name(state)]["max-age"] == 0
    assert JobRecord.objects.count() == 1


def test_an_expired_attempt_is_reported_as_expired_not_as_another_browser(world, module_on, ehr):
    browser, state = launched(world)
    AuthAttemptRecord.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
    r = browser.get(CALLBACK, {"state": state, "code": "c"})
    assert refused(r) == "attempt_invalid" and r.cookies[cookie_name(state)]["max-age"] == 0


@pytest.mark.parametrize(
    ("params", "reason"),
    [({"error": "access_denied", "error_description": "Call 555-0100 now"}, "declined"), ({}, "no_code")],
    ids=["declined", "no code"],
)
def test_a_declined_or_codeless_callback_creates_nothing_and_echoes_nothing(world, module_on, ehr, params, reason):
    browser, state = launched(world)
    r = browser.get(CALLBACK, {"state": state, **params})
    assert refused(r) == reason and "555" not in r["Location"]
    assert JobRecord.objects.count() == 0 and FhirSource.objects.count() == 0
    assert r.cookies[cookie_name(state)]["max-age"] == 0


def test_the_callback_ignores_any_django_login(world, module_on, ehr, user, organization):
    """The patient is the attempt's. A practitioner's login without the cookie is refused; the
    cookie with someone else's login completes, for the patient who launched."""
    token_response(ehr)
    _, state = launched(world)
    practitioner = Client()
    practitioner.force_login(user)
    assert refused(practitioner.get(CALLBACK, {"state": state, "code": "c"})) == "browser_mismatch"
    browser, state = launched(world)
    other = JheUser.objects.create_user(email="other@example.org", password="x", user_type="patient")
    browser.force_login(other)
    assert "route=status" in browser.get(CALLBACK, {"state": state, "code": "c"})["Location"]
    assert JobRecord.objects.get().participant_ref == str(world["patient"].id)


def test_an_ehr_patient_linked_to_someone_else_stops_the_pull(world, module_on, ehr):
    other = JheUser.objects.create_user(email="other@example.org", password="x", user_type="patient")
    PatientIdentifier.objects.create(patient=other.patient, system=ISS, value="ehr-p1")
    browser, state = launched(world)
    token_response(ehr)
    assert refused(browser.get(CALLBACK, {"state": state, "code": "c"})) == "identifier_conflict"
    assert JobRecord.objects.count() == 0 and FhirSource.objects.count() == 0


def test_consent_withdrawn_before_the_callback_creates_nothing(world, module_on, ehr):
    browser, state = launched(world)
    StudyPatientScopeConsent.objects.update(consented=False)
    token_response(ehr)
    assert refused(browser.get(CALLBACK, {"state": state, "code": "c"})) == "consent_withdrawn"
    assert JobRecord.objects.count() == 0


def test_the_module_off_at_the_callback_is_not_available(world):
    assert refused(Client().get(CALLBACK, {"state": "s", "code": "c"})) == "not_available"


def test_the_flow_sets_no_django_cookie(world, module_on, ehr):
    """No session, no CSRF cookie: the only cookie this client ever sets is the callback's."""
    token_response(ehr)
    browser = patient_browser(world)
    page = browser.get(CONNECT)
    started = launch(browser, world)
    returned = browser.get(CALLBACK, {"state": state_of(authorize_url(started)), "code": "c"})
    names = [*page.cookies, *started.cookies, *returned.cookies]
    assert names and all(name.startswith(CALLBACK_COOKIE_PREFIX) for name in names)


# --- the job endpoints -----------------------------------------------------------------------


def test_the_job_endpoint_shows_only_the_patients_own_job(world, module_on, ehr):
    browser, state = launched(world)
    token_response(ehr)
    browser.get(CALLBACK, {"state": state, "code": "c"})
    job_id = JobRecord.objects.get().id
    r = browser.get(f"/api/v1/uscdi-patient-access/jobs/{job_id}")
    assert r.status_code == 200 and r.json()["status"] == "queued" and r.json()["source"]["brand"] == "Example Health"
    assert r.json()["fhirSourceId"] == FhirSource.objects.get().id and "finishedAt" in r.json()
    assert Client().get(f"/api/v1/uscdi-patient-access/jobs/{job_id}").status_code == 401
    other = JheUser.objects.create_user(email="other@example.org", password="x", user_type="patient")
    other_world = dict(world, patient=other.patient)
    assert patient_browser(other_world).get(f"/api/v1/uscdi-patient-access/jobs/{job_id}").status_code == 404


def test_the_job_endpoint_answers_404_for_a_row_that_no_longer_reads_as_a_job(world, module_on):
    browser = patient_browser(world)
    row = JobRecord.objects.create(
        participant_ref=str(world["patient"].id),
        status="failed_terminal",
        body={"not": "a job"},
        credentials_enc="",
        created_at=timezone.now(),
    )
    assert browser.get(f"/api/v1/uscdi-patient-access/jobs/{row.id}").status_code == 404
    assert browser.get("/api/v1/uscdi-patient-access/jobs").status_code == 200


def test_a_malformed_source_filter_is_a_400_not_a_500(world):
    browser = patient_browser(world)
    assert browser.get("/api/v1/uscdi-patient-access/jobs", {"source": "²"}).status_code == 400
    assert browser.get("/api/v1/uscdi-patient-access/jobs", {"source": "abc"}).status_code == 400


# --- sign-out --------------------------------------------------------------------------------


def test_sign_out_revokes_the_access_token_and_its_refresh_token(world):
    headers = bearer(world["patient"].jhe_user, world["app"])
    access = AccessToken.objects.get(token=headers["HTTP_AUTHORIZATION"].removeprefix("Bearer "))
    refresh = RefreshToken.objects.create(
        user=access.user, application=access.application, token="refresh-1", access_token=access
    )
    browser = Client(enforce_csrf_checks=True, **headers)
    assert browser.get("/api/v1/users/profile").status_code == 200
    assert browser.get(SIGN_OUT).status_code == 405
    assert browser.post(SIGN_OUT).status_code == 204
    assert browser.get("/api/v1/users/profile").status_code == 401
    refresh.refresh_from_db()
    assert refresh.revoked is not None and not AccessToken.objects.filter(id=access.id).exists()


def test_sign_out_without_a_refresh_token_revokes_the_access_token(world):
    browser = Client(**bearer(world["patient"].jhe_user, world["app"]))
    assert browser.post(SIGN_OUT).status_code == 204
    assert browser.get("/api/v1/users/profile").status_code == 401
    assert Client().post(SIGN_OUT).status_code == 401


# --- settings, directory, guard --------------------------------------------------------------


def set_setting(key, value_type, value):
    s, _ = JheSetting.objects.get_or_create(key=key, defaults={"value_type": value_type})
    s.set_value(value_type, value)
    s.save()


@pytest.mark.parametrize(
    ("key", "value_type", "value", "message"),
    [
        ("patient_access.s3.layout", "string", "{job.id}/{attrs.patient_id}", "appended by the module"),
        ("patient_access.s3.layout", "string", "{attrs.ssn}", "not available"),
        ("patient_access.s3.layout", "string", "{attrs.patient_id}/{created_at:%Y}", "appended by the module"),
        ("patient_access.s3.layout", "string", "{job.__init__}", "private"),
        ("patient_access.s3.bucket", "string", "Not A Bucket", "bucket name"),
        ("patient_access.s3.encryption", "string", "rot13", "one of"),
        ("patient_access.fhir.extension_base_url", "string", "http://x.org/sd", "https URL ending in /"),
        ("patient_access.documents.max_count", "int", -1, "0 or more"),
        ("patient_access.documents.max_bytes_each", "int", 2**31, "32-bit"),
    ],
)
def test_settings_are_validated_when_saved(superuser, key, value_type, value, message):
    api = APIClient()
    api.default_format = "json"
    api.force_authenticate(superuser)
    r = api.post("/api/v1/jhe_settings", {"key": key, "valueType": value_type, "value": value})
    assert r.status_code == 400, r.content
    assert message in json.dumps(r.json())


@pytest.mark.parametrize(("debug", "status"), [(False, 400), (True, 201)])
def test_an_http_endpoint_is_accepted_only_in_debug(superuser, debug, status):
    api = APIClient()
    api.default_format = "json"
    api.force_authenticate(superuser)
    with override_settings(DEBUG=debug):
        r = api.post(
            "/api/v1/jhe_settings",
            {"key": "patient_access.s3.endpoint_url", "valueType": "string", "value": "http://s3:9000"},
        )
    assert r.status_code == status, r.content
    if not debug:
        assert "https" in json.dumps(r.json())


def test_the_settings_provider_reads_rows_with_defaults(db):
    with pytest.raises(ValueError, match="bucket"):
        read_patient_access_settings()
    set_setting("patient_access.s3.bucket", "string", "pulls")
    set_setting("patient_access.s3.endpoint_url", "string", "")
    s = read_patient_access_settings()
    assert s.s3.bucket == "pulls" and s.s3.endpoint_url is None and s.s3.layout == DEFAULT_LAYOUT
    assert s.bundle.extension_base_url == "https://jupyterhealth.org/fhir/StructureDefinition/"


def test_the_directory_maps_vendor_rows_to_the_registry(world):
    directory = JheOrmDirectory()
    selection = directory.selection(str(world["location"].id))
    settings = directory.settings_for(selection.endpoint)
    assert settings.registration.client_id == "client-1" and settings.fetch.name == "smart-reference"
    EhrVendor.objects.update(ehr_client_id="")
    with pytest.raises(KeyError):
        directory.settings_for(selection.endpoint)
    with pytest.raises(KeyError):
        directory.selection("99999")


def test_the_delivery_guard_refuses_after_consent_is_withdrawn(world, module_on, ehr):
    browser, state = launched(world)
    token_response(ehr)
    browser.get(CALLBACK, {"state": state, "code": "c"})
    job = module_on.jobs.get(JobRecord.objects.get().id)
    require_consent(job)
    StudyPatientScopeConsent.objects.update(consented=False)
    with pytest.raises(DeliveryRefused):
        require_consent(job)


# --- one pull, end to end --------------------------------------------------------------------


@pytest.fixture
def aws(monkeypatch):
    """Moto, started before the fake EHR: started after it, moto replaces the requests mock."""
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
        yield


def test_a_pull_lands_in_the_bucket_and_the_log_names_no_one(world, module_on, aws, ehr, caplog):
    set_setting("patient_access.s3.bucket", "string", BUCKET)
    set_setting("patient_access.s3.access_key_id", "string", "k")
    set_setting("patient_access.s3.secret_access_key", "string", "s")
    set_setting("patient_access.s3.region", "string", "us-east-1")
    browser, state = launched(world)
    token_response(ehr)
    ehr.add(
        responses.GET,
        f"{ISS}/Patient/ehr-p1",
        json={"resourceType": "Patient", "id": "ehr-p1", "name": [{"family": "Zwicky-Private"}]},
    )
    ehr.add(
        responses.GET,
        f"{ISS}/Observation",
        json={"resourceType": "Bundle", "entry": [{"resource": {"resourceType": "Observation", "id": "o1"}}]},
    )
    browser.get(CALLBACK, {"state": state, "code": "c"})
    job_id = JobRecord.objects.get().id
    with caplog.at_level(logging.DEBUG):
        assert module_on.runner.run(job_id)
    job = module_on.jobs.get(job_id)
    assert job.status is JobStatus.DONE, job.failure
    key = job.delivery.receipt.key
    source = FhirSource.objects.get()
    assert key.startswith(f"{world['patient'].id}/{source.id}/")
    body = boto3.client("s3", region_name="us-east-1").get_object(Bucket=BUCKET, Key=key)["Body"].read()
    composition = json.loads(body)["entry"][0]["resource"]
    assert composition["meta"]["source"].endswith(f"/fhir-source/{source.id}")
    assert browser.get(f"/api/v1/uscdi-patient-access/jobs/{job_id}").json()["delivered"] is True
    assert "Zwicky-Private" not in caplog.text and "ehr-token" not in caplog.text and "ehr-p1" not in caplog.text


# --- operator commands -----------------------------------------------------------------------


def test_the_check_command_probes_the_bucket(db, module_on, aws):
    import io

    from django.core.management import CommandError, call_command

    with pytest.raises(CommandError, match="bucket"):
        call_command("patient_access_check", stdout=io.StringIO())
    set_setting("patient_access.s3.bucket", "string", BUCKET)
    set_setting("patient_access.s3.access_key_id", "string", "k")
    set_setting("patient_access.s3.secret_access_key", "string", "s")
    out = io.StringIO()
    call_command("patient_access_check", stdout=out)
    assert "writable" in out.getvalue()
    assert boto3.client("s3", region_name="us-east-1").list_objects_v2(Bucket=BUCKET).get("KeyCount") == 0
    set_setting("patient_access.s3.bucket", "string", "missing-bucket")
    with pytest.raises(CommandError, match="missing-bucket"):
        call_command("patient_access_check", stdout=io.StringIO())


def test_the_sandbox_seed_builds_vendor_brands_and_locations(db, monkeypatch):
    import io

    from django.core.management import call_command

    monkeypatch.delenv("PATIENT_ACCESS_DEV_S3_BUCKET", raising=False)
    call_command("seed_patient_access_sandbox", stdout=io.StringIO())
    call_command("seed_patient_access_sandbox", stdout=io.StringIO())  # idempotent
    vendor = EhrVendor.objects.get(name="SMART Sandbox")
    assert vendor.ehr_client_id and EhrBrand.objects.filter(vendor=vendor).count() == 2
    assert all(b.fhir_base_url.startswith("https://launch.smarthealthit.org/v/r4/sim/") for b in vendor.brands.all())
    assert EhrBrandLocation.objects.filter(brand__vendor=vendor).count() == 2
    assert not JheSetting.objects.filter(key__startswith="patient_access.").exists()


def test_the_jobs_listing_is_the_patients_own_newest_first(world, module_on, ehr, organization):
    token_response(ehr)
    for _ in range(2):
        browser, state = launched(world)
        browser.get(CALLBACK, {"state": state, "code": "c"})
    listing = browser.get("/api/v1/uscdi-patient-access/jobs", {"source": world["ds"].id}).json()["results"]
    ids = list(JobRecord.objects.order_by("-created_at").values_list("id", flat=True))
    assert [j["id"] for j in listing] == ids
    assert browser.get("/api/v1/uscdi-patient-access/jobs", {"source": 999999}).json()["results"] == []
    other = JheUser.objects.create_user(email="other@example.org", password="x", user_type="patient")
    assert (
        patient_browser(dict(world, patient=other.patient)).get("/api/v1/uscdi-patient-access/jobs").json()["results"]
        == []
    )
    assert Client().get("/api/v1/uscdi-patient-access/jobs").status_code == 401
    logged_in = Client()
    logged_in.force_login(other)  # a Django login is not a token: this API takes tokens only
    assert logged_in.get("/api/v1/uscdi-patient-access/jobs").status_code == 401


def test_a_pull_whose_consent_is_withdrawn_before_delivery_writes_nothing(world, module_on, aws, ehr):
    set_setting("patient_access.s3.bucket", "string", BUCKET)
    set_setting("patient_access.s3.access_key_id", "string", "k")
    set_setting("patient_access.s3.secret_access_key", "string", "s")
    set_setting("patient_access.s3.region", "string", "us-east-1")
    browser, state = launched(world)
    token_response(ehr)
    ehr.add(responses.GET, f"{ISS}/Patient/ehr-p1", json={"resourceType": "Patient", "id": "ehr-p1"})
    ehr.add(responses.GET, f"{ISS}/Observation", json={"resourceType": "Bundle"})
    browser.get(CALLBACK, {"state": state, "code": "c"})
    StudyPatientScopeConsent.objects.update(consented=False)
    job_id = JobRecord.objects.get().id
    assert module_on.runner.run(job_id)
    job = module_on.jobs.get(job_id)
    assert job.status is JobStatus.FAILED_TERMINAL and job.failure_kind.value == "consent_withdrawn"
    assert boto3.client("s3", region_name="us-east-1").list_objects_v2(Bucket=BUCKET).get("KeyCount") == 0
    assert browser.get(f"/api/v1/uscdi-patient-access/jobs/{job_id}").json()["failureKind"] == "consent_withdrawn"


def test_the_seed_makes_the_client_invitable_for_the_demo_study(seeded):
    from django.conf import settings

    from core.models import StudyClient

    app = get_application_model().objects.get(name="USCDI Patient Access")
    assert app.jhe_client.invitation_url == f"{settings.SITE_URL}/clients/uscdi-patient-access/?code=CODE"
    ds = DataSource.objects.get(name="USCDI Patient Access")
    assert ClientDataSource.objects.filter(client=app, data_source=ds).exists()
    assert DataSourceSupportedScope.objects.filter(data_source=ds, scope_code__coding_code="*").exists()
    study_ids = set(StudyDataSource.objects.filter(data_source=ds).values_list("study_id", flat=True))
    assert study_ids and study_ids <= set(StudyClient.objects.filter(client=app).values_list("study_id", flat=True))


def test_rerunning_the_sandbox_seed_keeps_the_locations_a_pull_points_at(db, monkeypatch):
    """A FhirSource points at its location; deleting and recreating locations would detach it."""
    from django.core.management import call_command

    monkeypatch.delenv("PATIENT_ACCESS_DEV_S3_BUCKET", raising=False)
    call_command("seed_patient_access_sandbox", stdout=io.StringIO())
    before = set(EhrBrandLocation.objects.values_list("id", flat=True))
    call_command("seed_patient_access_sandbox", stdout=io.StringIO())
    assert set(EhrBrandLocation.objects.values_list("id", flat=True)) == before
