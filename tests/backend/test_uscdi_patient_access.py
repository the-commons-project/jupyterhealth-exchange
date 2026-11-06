"""USCDI Patient Access in JHE: the invitation's patient session, the launch, the callback's
order of checks, the job endpoint, the shell's endpoints in session mode, the settings keys, and
one pull end to end into a (moto) bucket."""

import io
import json
import logging
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import boto3
import pytest
import responses
from django.test import Client, override_settings
from django.utils import timezone
from moto import mock_aws
from oauth2_provider.models import get_application_model
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
    Patient,
    PatientIdentifier,
    PatientInvitation,
    StudyDataSource,
    StudyPatientScopeConsent,
    StudyScopeRequest,
)
from core.patient_access.consent import require_consent
from core.patient_access.directory import JheOrmDirectory
from core.patient_access.settings import read_patient_access_settings
from core.views.uscdi_patient_access import FLOW_ERROR_KEY
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


def invitation_code(world):
    invitation = PatientInvitation.issue(world["patient"], world["app"])
    return f"testserver_{invitation.token}"


def patient_browser(world) -> Client:
    browser = Client(enforce_csrf_checks=True)
    r = browser.get(CONNECT, {"code": invitation_code(world)})
    assert r.status_code == 302 and "code=" not in r["Location"]
    return browser


def csrf(browser: Client) -> str:
    browser.get(CONNECT)
    return browser.cookies["csrftoken"].value


LAUNCH = "/clients/uscdi-patient-access/launch"
ERROR_ROUTE = f"{CONNECT}?route=error"


def launch(browser: Client, world, scopes=("patient/Observation.read",), **fields):
    """Submit the connect form as the browser does: form-encoded, the CSRF token as a field."""
    data = {
        "csrfmiddlewaretoken": csrf(browser),
        "location_id": world["location"].id,
        "data_source_id": world["ds"].id,
        "scopes": list(scopes),
        **fields,
    }
    return browser.post(LAUNCH, data)


def authorize_url(r) -> str:
    assert r.status_code == 302, r.content
    assert r["Location"].startswith(AUTHORIZE), r["Location"]
    return r["Location"]


def flow_error(browser: Client):
    return browser.session.get(FLOW_ERROR_KEY)


# --- the invitation's session ----------------------------------------------------------------


def test_the_invitation_logs_the_patient_in_and_strips_the_code(world):
    browser = patient_browser(world)
    assert int(browser.session["_auth_user_id"]) == world["patient"].jhe_user_id
    assert browser.session.get_expiry_age() <= 12 * 3600
    assert browser.get(CONNECT).context["config"]["auth"] == "session"


@pytest.mark.parametrize("code", ["testserver_nope", "garbage"])
def test_a_bad_invitation_gets_the_error_route(world, code):
    browser = Client()
    r = browser.get(CONNECT, {"code": code})
    assert r["Location"].endswith("?route=error")
    assert flow_error(browser)["title"] == "This invitation link isn't valid"
    assert "_auth_user_id" not in browser.session


def test_an_invitation_for_another_client_does_not_log_in(world):
    other = get_application_model().objects.create(
        name="Other", client_type="public", authorization_grant_type="authorization-code"
    )
    invitation = PatientInvitation.issue(world["patient"], other)
    browser = Client()
    browser.get(CONNECT, {"code": f"testserver_{invitation.token}"})
    assert "_auth_user_id" not in browser.session


def test_a_reused_invitation_works_within_the_window_and_not_after(world):
    code = invitation_code(world)
    assert Client().get(CONNECT, {"code": code})["Location"].startswith(CONNECT)
    second = Client()
    second.get(CONNECT, {"code": code})
    assert "_auth_user_id" in second.session
    PatientInvitation.objects.update(last_updated=timezone.now() - timedelta(hours=13))
    third = Client()
    third.get(CONNECT, {"code": code})
    assert "_auth_user_id" not in third.session


def test_without_a_session_the_page_says_so(world, module_on):
    config = Client().get(CONNECT).context["config"]
    assert config["flowError"]["title"] == "Your session has ended"


# --- launch ----------------------------------------------------------------------------------


def test_launch_redirects_to_the_authorize_url_and_records_the_attempt(world, module_on, ehr):
    url = authorize_url(launch(patient_browser(world), world))
    q = parse_qs(urlparse(url).query)
    assert q["client_id"] == ["client-1"]
    assert q["redirect_uri"] == ["http://testserver/clients/uscdi-patient-access/callback"]
    attempt = AuthAttemptRecord.objects.get()
    assert attempt.participant_ref == str(world["patient"].id)
    assert attempt.body["scopes_declined"] == ["patient/Patient.read"]


def test_the_page_tells_the_client_which_vendors_ask_per_type_themselves(world, module_on):
    config = patient_browser(world).get(CONNECT).context["config"]
    assert config["launchUrl"] == LAUNCH
    assert config["typeChoiceByVendor"]["Epic Sandbox"] == "ehr"
    assert config["typeChoiceByVendor"]["SMART Sandbox"] == "host"


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


def test_launch_without_a_patient_session_goes_to_the_error_route(world, module_on, ehr):
    anonymous = Client(enforce_csrf_checks=True)
    r = launch(anonymous, world)
    assert r.status_code == 302 and r["Location"] == ERROR_ROUTE
    assert flow_error(anonymous)["title"] == "Your session has ended"
    assert not AuthAttemptRecord.objects.exists()


def test_launch_needs_the_csrf_token(world, module_on, ehr):
    browser = patient_browser(world)
    r = browser.post(LAUNCH, {"location_id": world["location"].id, "data_source_id": world["ds"].id})
    assert r.status_code == 403
    assert not AuthAttemptRecord.objects.exists()


def test_launch_is_a_post(world, module_on, ehr):
    assert patient_browser(world).get(LAUNCH).status_code == 405


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda world: StudyPatientScopeConsent.objects.update(consented=False), "Please agree"),
        (
            lambda world: {"data_source_id": DataSource.objects.create(name="Other", type="patient_app").id},
            "not available",
        ),
        (lambda world: {"data_source_id": "not-a-number"}, "choose your healthcare organization again"),
        (lambda world: {"location_id": "999999"}, "not in the directory"),
    ],
    ids=["no consent", "another client's data source", "malformed form", "unknown facility"],
)
def test_a_refused_launch_goes_to_the_error_route_and_records_nothing(world, module_on, ehr, change, message):
    fields = change(world)
    browser = patient_browser(world)
    r = launch(browser, world, **(fields if isinstance(fields, dict) else {}))
    assert r.status_code == 302 and r["Location"] == ERROR_ROUTE
    assert flow_error(browser)["title"] == "We couldn't start the connection"
    assert message in flow_error(browser)["message"]
    assert not AuthAttemptRecord.objects.exists()


def test_the_module_off_sends_the_launch_to_the_error_route_and_the_page_says_so(world, ehr):
    browser = patient_browser(world)
    r = launch(browser, world)
    assert r.status_code == 302 and r["Location"] == ERROR_ROUTE
    assert browser.get(CONNECT).context["config"]["flowError"]["title"] == "Not available"


# --- callback --------------------------------------------------------------------------------


def launched(world):
    browser = patient_browser(world)
    state = parse_qs(urlparse(authorize_url(launch(browser, world))).query)["state"][0]
    return browser, state


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


def test_the_callback_creates_the_pull_and_its_records(world, module_on, ehr):
    browser, state = launched(world)
    token_response(ehr)
    r = browser.get(CALLBACK, {"state": state, "code": "c1"})
    assert r.status_code == 302 and "route=status" in r["Location"]
    job = JobRecord.objects.get()
    source = FhirSource.objects.get()
    assert source.ehr_brand_location == world["location"] and source.label == "SMART Sandbox - Example Health"
    assert job.body["participant"]["attrs"]["fhir_source_id"] == source.id
    assert job.body["participant"]["attrs"]["study_ids"] == [world["study"].id]
    assert PatientIdentifier.objects.filter(system=ISS, value="ehr-p1", patient=world["patient"]).exists()
    assert "ehr-token" not in job.credentials_enc


def test_a_second_pull_reuses_the_fhir_source(world, module_on, ehr):
    token_response(ehr)
    for _ in range(2):
        browser, state = launched(world)
        browser.get(CALLBACK, {"state": state, "code": "c"})
    assert FhirSource.objects.count() == 1 and JobRecord.objects.count() == 2


def test_another_patients_session_is_refused_and_the_code_never_spent(world, module_on, ehr, organization):
    _, state = launched(world)
    other = JheUser.objects.create_user(email="other@example.org", password="x", user_type="patient")
    other.patient.organizations.add(organization)
    intruder = Client()
    intruder.force_login(other)
    r = intruder.get(CALLBACK, {"state": state, "code": "stolen"})
    assert r["Location"].endswith("?route=error")
    assert not any(c.request.method == "POST" for c in ehr.calls)
    assert JobRecord.objects.count() == 0
    assert AuthAttemptRecord.objects.get().used_at is not None  # spent: the real patient starts again


def test_no_session_at_the_callback_is_refused(world, module_on, ehr):
    _, state = launched(world)
    r = Client().get(CALLBACK, {"state": state, "code": "c"})
    assert r["Location"].endswith("?route=error") and JobRecord.objects.count() == 0


@pytest.mark.parametrize("params", [{"error": "access_denied"}, {}])
def test_a_declined_or_codeless_callback_creates_nothing(world, module_on, ehr, params):
    browser, state = launched(world)
    r = browser.get(CALLBACK, {"state": state, **params})
    assert r["Location"].endswith("?route=error")
    assert JobRecord.objects.count() == 0 and FhirSource.objects.count() == 0


def test_a_replayed_callback_is_refused(world, module_on, ehr):
    browser, state = launched(world)
    token_response(ehr)
    browser.get(CALLBACK, {"state": state, "code": "c"})
    r = browser.get(CALLBACK, {"state": state, "code": "c"})
    assert r["Location"].endswith("?route=error") and JobRecord.objects.count() == 1


def test_an_ehr_patient_linked_to_someone_else_stops_the_pull(world, module_on, ehr, organization):
    other = JheUser.objects.create_user(email="other@example.org", password="x", user_type="patient")
    PatientIdentifier.objects.create(patient=other.patient, system=ISS, value="ehr-p1")
    browser, state = launched(world)
    token_response(ehr)
    r = browser.get(CALLBACK, {"state": state, "code": "c"})
    assert r["Location"].endswith("?route=error")
    carried = flow_error(browser)
    assert "already linked" in carried["message"] and carried["retry"] is False  # trying again cannot help
    assert JobRecord.objects.count() == 0 and FhirSource.objects.count() == 0


def test_consent_withdrawn_before_the_callback_creates_nothing(world, module_on, ehr):
    browser, state = launched(world)
    StudyPatientScopeConsent.objects.update(consented=False)
    token_response(ehr)
    r = browser.get(CALLBACK, {"state": state, "code": "c"})
    assert r["Location"].endswith("?route=error") and JobRecord.objects.count() == 0


# --- the job endpoint ------------------------------------------------------------------------


def test_the_job_endpoint_shows_only_the_patients_own_job(world, module_on, ehr, organization):
    browser, state = launched(world)
    token_response(ehr)
    browser.get(CALLBACK, {"state": state, "code": "c"})
    job_id = JobRecord.objects.get().id
    r = browser.get(f"/api/v1/uscdi-patient-access/jobs/{job_id}")
    assert r.status_code == 200 and r.json()["status"] == "queued" and r.json()["source"]["brand"] == "Example Health"
    assert r.json()["fhirSourceId"] == FhirSource.objects.get().id and "finishedAt" in r.json()
    other = JheUser.objects.create_user(email="other@example.org", password="x", user_type="patient")
    stranger = Client()
    stranger.force_login(other)  # logged in, but not through an invitation: nobody here
    assert stranger.get(f"/api/v1/uscdi-patient-access/jobs/{job_id}").status_code == 403
    assert Client().get(f"/api/v1/uscdi-patient-access/jobs/{job_id}").status_code == 403
    other_world = dict(world, patient=other.patient)
    assert patient_browser(other_world).get(f"/api/v1/uscdi-patient-access/jobs/{job_id}").status_code == 404


# --- the shell's calls: this client's own routes, never the shared ones ---------------------


SHARED_ROUTES = ("/api/v1/users/profile", "consents", "/api/v1/fhir_sources", "/api/v1/ehr-patient-portal/brands")


def shared(route, patient):
    return f"/api/v1/patients/{patient.id}/consents" if route == "consents" else route


def bearer(user, app):
    """A real OAuth access token: the shared routes authenticate tokens, not sessions."""
    from oauth2_provider.models import AccessToken

    token = AccessToken.objects.create(
        user=user,
        application=app,
        token=f"token-{user.id}",
        expires=timezone.now() + timedelta(hours=1),
        scope="openid",
    )
    return {"HTTP_AUTHORIZATION": f"Bearer {token.token}"}


def test_the_clients_own_routes_serve_the_shell(world):
    browser = patient_browser(world)
    assert browser.get("/api/v1/uscdi-patient-access/profile").json()["patient"]["id"] == world["patient"].id
    consents = browser.get("/api/v1/uscdi-patient-access/consents")
    assert consents.status_code == 200 and consents.json()["patient"]["id"] == world["patient"].id
    assert browser.get("/api/v1/uscdi-patient-access/fhir-sources").status_code == 200
    assert browser.get("/api/v1/uscdi-patient-access/brands", {"q": "Example"}).json()["results"]
    body = {"study_scope_consents": []}
    no_csrf = browser.patch("/api/v1/uscdi-patient-access/consents", data=body, content_type="application/json")
    assert no_csrf.status_code == 403
    with_csrf = browser.patch(
        "/api/v1/uscdi-patient-access/consents",
        data=body,
        content_type="application/json",
        HTTP_X_CSRFTOKEN=csrf(browser),
    )
    assert with_csrf.status_code == 200


def test_the_shared_routes_refuse_the_clients_session(world):
    """JHE's shared routes are token-only, exactly as before this client existed."""
    browser = patient_browser(world)
    for route in SHARED_ROUTES:
        assert browser.get(shared(route, world["patient"])).status_code in (401, 403), route


def test_tokens_still_work_on_all_four_shared_routes(world):
    patient = world["patient"]
    headers = bearer(patient.jhe_user, world["app"])
    for route in SHARED_ROUTES:
        assert Client().get(shared(route, patient), **headers).status_code == 200, route


def test_a_dead_token_never_falls_through_to_the_session(world):
    """A shared device: an old tab's expired token, and another patient's session in the cookie jar."""
    browser = patient_browser(world)
    dead = {"HTTP_AUTHORIZATION": "Bearer expired-or-unknown"}
    assert browser.get("/api/v1/users/profile", **dead).status_code == 401
    assert browser.get("/api/v1/uscdi-patient-access/profile", **dead).status_code in (401, 403)


def test_a_practitioner_who_is_also_a_patient_uses_their_link_like_any_patient(world, user, organization):
    """The mark says whose session this is; what else the user is does not matter. Through the
    link they get this client's routes for their own patient, and nothing else by that session."""
    dual = Patient.objects.create(jhe_user=user, name_family="Dual", name_given="D")
    add_patient_to_study(dual, world["study"])
    browser = patient_browser(dict(world, patient=dual))
    assert browser.get("/api/v1/uscdi-patient-access/profile").json()["patient"]["id"] == dual.id
    for route in ("consents", "fhir-sources", "brands"):
        assert browser.get(f"/api/v1/uscdi-patient-access/{route}").status_code == 200, route
    assert browser.get(f"/api/v1/patients/{world['patient'].id}/consents").status_code in (401, 403)
    assert browser.get(f"/api/v1/patients/{dual.id}/consents").status_code in (401, 403)  # shared: token-only
    allauth = Client()
    allauth.force_login(user)  # the ordinary practitioner login: no mark at all
    assert allauth.get("/api/v1/uscdi-patient-access/consents").status_code in (401, 403)


def test_a_failed_link_ends_whatever_session_the_browser_had(world):
    """A shared device: patient A used it, patient B's link is dead. Back from the error screen
    must not open A's hub."""
    browser = patient_browser(world)
    assert browser.get("/api/v1/uscdi-patient-access/profile").status_code == 200
    r = browser.get(CONNECT, {"code": "testserver_nope"})
    assert r["Location"].endswith("?route=error")
    assert "_auth_user_id" not in browser.session
    assert browser.get("/api/v1/uscdi-patient-access/profile").status_code in (401, 403)
    assert flow_error(browser)["title"] == "This invitation link isn't valid"


def test_the_authorize_page_refuses_the_invitation_session(world, user):
    """The session is the link's, for this client's routes: it can never be exchanged for a
    token, for any application. A practitioner's own login is unaffected."""
    query = {
        "client_id": world["app"].client_id,
        "response_type": "code",
        "redirect_uri": "http://testserver/auth/callback",
        "code_challenge": "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
        "code_challenge_method": "S256",
    }
    browser = patient_browser(world)
    assert browser.get("/o/authorize/", query).status_code == 403
    assert browser.post("/o/authorize/", query).status_code == 403
    allauth = Client()
    allauth.force_login(user)
    assert allauth.get("/o/authorize/", query).status_code != 403


def test_the_job_endpoint_answers_404_for_a_row_that_no_longer_reads_as_a_job(world, module_on):
    from patient_access.django_app.models import JobRecord as Row

    browser = patient_browser(world)
    row = Row.objects.create(
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
    assert browser.get("/api/v1/uscdi-patient-access/jobs", {"source": "\u00b2"}).status_code == 400
    assert browser.get("/api/v1/uscdi-patient-access/jobs", {"source": "abc"}).status_code == 400


def test_a_link_cannot_reset_a_patients_consents(world):
    """The shared view's GET ?reset=true deletes consents: a cookie must never reach it."""
    browser = patient_browser(world)
    before = StudyPatientScopeConsent.objects.count()
    assert browser.get("/api/v1/uscdi-patient-access/consents", {"reset": "true"}).status_code == 400
    assert browser.get(f"/api/v1/patients/{world['patient'].id}/consents", {"reset": "true"}).status_code in (401, 403)
    assert StudyPatientScopeConsent.objects.count() == before > 0


def test_a_patient_session_cannot_write_fhir_sources(world):
    browser = patient_browser(world)
    r = browser.post(
        "/api/v1/fhir_sources",
        data={"dataSource": world["ds"].id},
        content_type="application/json",
        HTTP_X_CSRFTOKEN=csrf(browser),
    )
    assert r.status_code in (401, 403)
    assert browser.post("/api/v1/uscdi-patient-access/fhir-sources", HTTP_X_CSRFTOKEN=csrf(browser)).status_code == 405


def test_a_patient_session_gets_nothing_from_the_practitioner_api(world):
    browser = patient_browser(world)
    assert browser.get("/api/v1/organizations").status_code in (401, 403)
    assert browser.get("/admin/").status_code == 302  # to the admin login


def test_the_session_cookie_rides_a_top_level_redirect():
    """The USCDI callback is a top-level GET redirect from the EHR's site; SameSite=Strict would
    drop the session cookie and every callback would fail. Lax is required: do not tighten it."""
    from django.conf import settings

    assert settings.SESSION_COOKIE_SAMESITE == "Lax"
    assert settings.CSRF_COOKIE_SAMESITE == "Lax"


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


def test_other_clients_invitations_still_redeem_for_a_grant(world):
    """The generic endpoint refuses only this client's invitations; the EHR Patient Portal's and
    the rest keep working as before."""
    other = get_application_model().objects.create(
        name="Other", client_type="public", authorization_grant_type="authorization-code"
    )
    invitation = PatientInvitation.issue(world["patient"], other)
    r = Client().post(f"/api/v1/invitation/{invitation.token}")
    assert r.status_code == 200, r.content
    assert r.json()["grant"]["code"] and r.json()["grant"]["client_id"] == other.client_id
    assert PatientInvitation.objects.get(id=invitation.id).status == PatientInvitation.Status.REDEEMED


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
    stranger = Client()
    stranger.force_login(other)  # not through an invitation
    assert stranger.get("/api/v1/uscdi-patient-access/jobs").status_code == 403


# --- invitations, sign-out, the error screen, the launch's refusals --------------------------


def test_the_reuse_window_runs_from_first_use_however_often_the_link_is_opened(world):
    code = invitation_code(world)
    patient_link = Client().get(CONNECT, {"code": code})
    assert "?route=error" not in patient_link["Location"]
    invitation = PatientInvitation.objects.get()
    PatientInvitation.objects.filter(id=invitation.id).update(last_updated=timezone.now() - timedelta(hours=11))
    first_use = PatientInvitation.objects.get().last_updated
    again = Client()
    again.get(CONNECT, {"code": code})
    assert "_auth_user_id" in again.session  # inside the window
    assert PatientInvitation.objects.get().last_updated == first_use  # and the window did not restart
    PatientInvitation.objects.filter(id=invitation.id).update(last_updated=timezone.now() - timedelta(hours=13))
    late = Client()
    late.get(CONNECT, {"code": code})
    assert "_auth_user_id" not in late.session


@pytest.mark.parametrize("state", ["expired", "cancelled", "reissued"])
def test_a_dead_invitation_gets_the_error_route(world, state):
    code = invitation_code(world)
    invitation = PatientInvitation.objects.get()
    if state == "reissued":
        PatientInvitation.issue(world["patient"], world["app"])
    elif state == "expired":
        PatientInvitation.objects.filter(id=invitation.id).update(last_updated=timezone.now() - timedelta(days=30))
    else:
        PatientInvitation.objects.filter(id=invitation.id).update(status=PatientInvitation.Status.CANCELLED)
    browser = Client()
    r = browser.get(CONNECT, {"code": code})
    assert r["Location"].endswith("?route=error") and "_auth_user_id" not in browser.session


def test_this_clients_invitation_cannot_be_turned_into_a_token(world, api_client):
    invitation = PatientInvitation.issue(world["patient"], world["app"])
    r = Client().post(f"/api/v1/invitation/{invitation.token}")
    assert r.status_code == 403
    assert PatientInvitation.objects.get(id=invitation.id).status == PatientInvitation.Status.ISSUED


def test_sign_out_ends_the_session(world):
    browser = patient_browser(world)
    assert browser.get("/clients/uscdi-patient-access/sign-out").status_code == 405
    r = browser.post("/clients/uscdi-patient-access/sign-out", {"csrfmiddlewaretoken": csrf(browser)})
    assert r.status_code == 302 and r["Location"].endswith("?route=error")
    assert "_auth_user_id" not in browser.session and flow_error(browser)["title"] == "You're signed out"
    assert browser.get("/api/v1/uscdi-patient-access/profile").status_code in (401, 403)


def test_the_error_screen_survives_a_reload_and_clears_when_the_patient_moves_on(world, module_on, ehr):
    browser = patient_browser(world)
    launch(browser, world, location_id="999999")
    error_route = {"route": "error"}
    first = browser.get(CONNECT, error_route).context["config"]["flowError"]
    assert first["title"] == "We couldn't start the connection"
    assert browser.get(CONNECT, error_route).context["config"]["flowError"] == first  # a reload
    assert browser.get(CONNECT, {"route": "hub"}).context["config"]["flowError"] is None
    assert browser.get(CONNECT, error_route).context["config"]["flowError"] is None


def test_an_empty_checklist_is_refused(world, module_on, ehr):
    browser = patient_browser(world)
    r = launch(browser, world, scopes=())
    assert r["Location"] == ERROR_ROUTE and "at least one" in flow_error(browser)["message"]
    assert not AuthAttemptRecord.objects.exists()


def test_a_vendor_without_a_client_id_is_not_available(world, module_on, ehr):
    EhrVendor.objects.update(ehr_client_id="")
    browser = patient_browser(world)
    r = launch(browser, world)
    assert r["Location"] == ERROR_ROUTE and "not available" in flow_error(browser)["message"]


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
