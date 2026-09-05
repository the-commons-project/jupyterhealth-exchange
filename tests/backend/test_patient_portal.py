"""The patient-facing hub (/patient/) and its consent / manage / done screens.

These run against the seeded client graph: the EHR Patient Portal and Open Wearables clients
are patient-facing, CareX is not, and Oura is shared across clients.
"""

from datetime import timedelta
from unittest import mock
from urllib.parse import quote

import pytest
from django.utils import timezone
from oauth2_provider.models import get_application_model

from core.models import (
    CodeableConcept,
    DataSource,
    EhrBrandLocation,
    FhirAuxResource,
    FhirSource,
    Patient,
    PatientInvitation,
    Study,
    StudyPatient,
    StudyPatientScopeConsent,
)
from core.views.patient_portal import (
    SESSION_INVITATION_KEY,
    SESSION_LAST_DS_KEY,
    _invitation_from_code,
    _patient_label,
    _scope_detail,
    _sources,
)

from .utils import card_block, mint_invitation_code

Application = get_application_model()
FACILITY = "Epic Sandbox - Madison Campus"


@pytest.fixture
def pamela(seeded):
    return Patient.objects.get(jhe_user__email="ll_patient_pamela@example.com")


@pytest.fixture
def peter(seeded):
    return Patient.objects.get(jhe_user__email="ll_patient_peter@example.com")


@pytest.fixture
def ehr_app(seeded):
    return Application.objects.get(name="EHR Patient Portal")


@pytest.fixture
def ow_app(seeded):
    return Application.objects.get(name="Open Wearables")


@pytest.fixture
def ehr_ds(seeded):
    return DataSource.objects.get(name="EHR Patient Portal")


@pytest.fixture
def oura_ds(seeded):
    return DataSource.objects.get(name="Oura")


@pytest.fixture
def star(seeded):
    return CodeableConcept.objects.get(coding_system="http://hl7.org/fhir/resource-types", coding_code="*")


@pytest.fixture
def sleep_code(seeded):
    return CodeableConcept.objects.get(coding_code="ieee:sleep-episode:1.0")


@pytest.fixture
def code(pamela, ehr_app):
    return mint_invitation_code(pamela, ehr_app)


@pytest.fixture
def portal(client, code):
    """A client whose session carries Pamela's EHR Patient Portal invitation."""
    assert client.get(f"/patient/?code={code}").status_code == 200
    return client


@pytest.fixture
def consented(portal, ehr_ds):
    """`portal` after Pamela consented to the EHR Patient Portal's clinical records."""
    assert portal.post(f"/patient/consent/{ehr_ds.id}/").status_code == 302
    return portal


@pytest.fixture
def fhir_source(pamela, ehr_ds):
    location = EhrBrandLocation.objects.get(name=FACILITY)
    fhir_source = FhirSource.objects.create(patient=pamela, data_source=ehr_ds, ehr_brand_location=location)
    for _ in range(3):
        FhirAuxResource.objects.create(fhir_source=fhir_source, resource_type="Observation")
    return fhir_source


def _star_consent(patient, star):
    study = Study.objects.get(name="Lifespan Study on BP & HR")
    return StudyPatientScopeConsent.objects.filter(
        study_patient__study=study, study_patient__patient=patient, scope_code=star
    )


def _card(resp, title):
    return next(card for card in resp.context["cards"] if card["title"] == title)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Sleep episode (IEEE)", "Sleep episode"),
        ("Heart Rate (OMH)", "Heart Rate"),
        ("Clinical records", "Clinical records"),
        ("A (b) c", "A (b) c"),
        (None, ""),
        ("", ""),
    ],
)
def test_patient_label_strips_one_trailing_parenthetical(text, expected):
    assert _patient_label(text) == expected


def test_invitation_from_code_resolves_patient_without_redeeming(pamela, ehr_app, code):
    inv = _invitation_from_code(code)
    assert (inv.patient_id, inv.client_id, inv.status) == (pamela.id, ehr_app.id, PatientInvitation.Status.ISSUED)


@pytest.mark.parametrize("bad_code", ["", "garbage", "localhost%3A8001_nope"])
def test_landing_rejects_invalid_codes(db, client, bad_code):
    resp = client.get(f"/patient/?code={bad_code}")
    assert resp.status_code == 400
    assert "invitation" in resp.content.decode().lower()


def test_valid_code_starts_a_fresh_capped_session_that_outlives_the_code(client, code, pamela, ehr_app):
    session = client.session
    session["x"] = 1
    session.save()
    old_key = session.session_key

    assert client.get(f"/patient/?code={code}").status_code == 200

    inv = PatientInvitation.objects.get(patient=pamela, client=ehr_app, status=PatientInvitation.Status.ISSUED)
    assert client.session.session_key != old_key  # rotated, so a planted pre-auth session can't be fixated
    assert client.session[SESSION_INVITATION_KEY] == inv.pk
    assert 0 < client.session.get_expiry_age() <= 12 * 3600
    resp = client.get("/patient/")
    assert resp.status_code == 200
    assert "EHR Patient Portal" in resp.content.decode()


@pytest.mark.parametrize(
    ("status", "age", "expected"),
    [
        (PatientInvitation.Status.ISSUED, timedelta(days=8), 400),
        (PatientInvitation.Status.REDEEMED, timedelta(hours=13), 400),
        (PatientInvitation.Status.REDEEMED, timedelta(hours=1), 200),
    ],
)
def test_landing_applies_the_invitation_expiry_windows(client, code, status, age, expected):
    PatientInvitation.objects.update(status=status, last_updated=timezone.now() - age)
    assert client.get(f"/patient/?code={code}").status_code == expected


@pytest.mark.parametrize(
    "revoke",
    [
        pytest.param(
            lambda inv: PatientInvitation.objects.filter(pk=inv.pk).update(status="cancelled"), id="cancelled"
        ),
        pytest.param(lambda inv: PatientInvitation.build_link(inv.patient, inv.client), id="reissued"),
    ],
)
def test_codeless_visit_after_revocation_is_rejected_and_session_cleared(portal, revoke):
    revoke(PatientInvitation.objects.get(pk=portal.session[SESSION_INVITATION_KEY]))

    assert portal.get("/patient/").status_code == 400
    assert SESSION_INVITATION_KEY not in portal.session


def test_consent_post_rejects_expired_code_and_creates_no_consent(client, code, pamela, ehr_ds, star):
    PatientInvitation.objects.update(last_updated=timezone.now() - timedelta(days=8))

    assert client.post(f"/patient/consent/{ehr_ds.id}/", {"code": code}).status_code == 400
    assert not _star_consent(pamela, star).exists()


def test_landing_lists_ehr_source_as_not_consented(portal, ehr_ds):
    resp = portal.get("/patient/")

    html = resp.content.decode()
    assert "Choose how to share your data" in html
    assert "pf-back" not in html
    assert _card(resp, "EHR Patient Portal") == {
        "title": "EHR Patient Portal",
        "desc": "Clinical records",
        "icon": "bi-file-earmark-text",
        "on": False,
        "badge": "Not consented",
        "url": f"/patient/consent/{ehr_ds.id}/",
    }
    card = card_block(html, "EHR Patient Portal")
    assert "Not consented" in card and "pf-card__badge--on" not in card


def test_peters_landing_lists_only_patient_facing_sources(client, peter, ehr_app):
    resp = client.get(f"/patient/?code={mint_invitation_code(peter, ehr_app)}")

    assert resp.status_code == 200
    assert resp.context["eyebrow"] == "Your studies"
    assert [c["title"] for c in resp.context["cards"]] == ["EHR Patient Portal", "Oura"]  # no CareX/Questionnaire
    oura = _card(resp, "Oura")
    assert (oura["on"], oura["badge"], oura["desc"]) == (True, "Consented", "Sleep episode")
    assert "pf-card__badge--on" in card_block(resp.content.decode(), "Oura")


def test_consented_card_advertises_the_fhir_source_until_revoked(consented, pamela, ehr_ds, fhir_source):
    card = _card(consented.get("/patient/"), "EHR Patient Portal")
    assert (card["on"], card["badge"], card["url"]) == (True, "Consented", f"/patient/manage/{ehr_ds.id}/")
    assert card["desc"] == f"{FACILITY} · Clinical records · 3 records"

    assert consented.post(f"/patient/manage/{ehr_ds.id}/").status_code == 302

    card = _card(consented.get("/patient/"), "EHR Patient Portal")
    assert (card["on"], card["badge"], card["desc"]) == (False, "Not consented", "Clinical records")
    assert FhirSource.objects.filter(patient=pamela, data_source=ehr_ds).exists()  # retained, just not advertised


def test_consent_get_lists_pending_clinical_records(portal, ehr_ds):
    resp = portal.get(f"/patient/consent/{ehr_ds.id}/")

    assert resp.status_code == 200
    assert resp.context["rows"] == ["Clinical records"]
    assert resp.context["scope_detail"].startswith("Demographics, ")
    assert "observations" in resp.context["scope_detail"]
    html = resp.content.decode()
    assert "Agree and share" in html and "csrfmiddlewaretoken" in html
    assert 'name="code"' not in html  # the session carries the invitation
    assert "pf-back" in html and 'href="/patient/"' in html


def test_scope_detail_is_empty_for_a_client_with_no_smart_scopes(oura_ds):
    assert _scope_detail(oura_ds.id) == ""


def test_consenting_oura_asks_for_and_records_only_the_sleep_study_scope(portal, pamela, oura_ds, sleep_code):
    hr_code = CodeableConcept.objects.get(coding_code="omh:heart-rate:2.0")
    rows = StudyPatientScopeConsent.objects.filter(study_patient__patient=pamela)
    rows.filter(scope_code__in=[hr_code, sleep_code]).update(consented=False)

    resp = portal.get(f"/patient/consent/{oura_ds.id}/")
    assert resp.status_code == 200
    assert resp.context["rows"] == ["Sleep episode"]  # Heart Rate is collected via CareX; "(IEEE)" stripped
    assert "(IEEE)" not in resp.content.decode()

    assert portal.post(f"/patient/consent/{oura_ds.id}/").status_code == 302
    assert rows.filter(scope_code=sleep_code, consented=True).exists()
    assert not rows.filter(scope_code=hr_code, consented=True).exists()


def test_consent_post_records_consent_and_routes_to_the_connect_page(portal, code, pamela, ehr_app, ehr_ds, star):
    resp = portal.post(f"/patient/consent/{ehr_ds.id}/")

    assert resp.status_code == 302
    assert resp.url == f"/clients/ehr-patient-portal/?code={code}"
    assert _star_consent(pamela, star).get().consented is True
    assert next(s for s in _sources(pamela) if s["id"] == ehr_ds.id)["connected"] is True
    assert PatientInvitation.objects.get(patient=pamela, client=ehr_app).status == PatientInvitation.Status.ISSUED


def test_consent_on_a_source_with_nothing_pending_redirects_to_the_hub(consented, pamela, ehr_ds, star):
    for method in (consented.get, consented.post):
        resp = method(f"/patient/consent/{ehr_ds.id}/")
        assert (resp.status_code, resp.url) == (302, "/patient/")
    assert _star_consent(pamela, star).count() == 1


def test_consent_post_without_code_or_session_is_invalid(db, client):
    assert client.post("/patient/consent/1/").status_code == 400


@pytest.fixture
def peter_oura_pending(peter, sleep_code):
    """Peter's seeded Oura consent revoked, so the sleep scope is pending again."""
    study = Study.objects.get(name="Lifespan Study on Sleep & BP")
    study_patient = StudyPatient.objects.get(study=study, patient=peter)
    rows = StudyPatientScopeConsent.objects.filter(study_patient=study_patient, scope_code=sleep_code)
    rows.update(consented=False)
    return rows


def test_consent_post_cross_client_mints_the_sources_own_invitation(
    client, peter, ehr_app, ow_app, oura_ds, peter_oura_pending
):
    code = mint_invitation_code(peter, ehr_app)

    resp = client.post(f"/patient/consent/{oura_ds.id}/", {"code": code})

    assert resp.status_code == 302
    assert resp.url.startswith("http://localhost:8001/clients/ow/launch?code=")
    assert PatientInvitation.objects.get(patient=peter, client=ow_app).status == PatientInvitation.Status.ISSUED
    assert peter_oura_pending.get().consented is True
    assert next(s for s in _sources(peter) if s["id"] == oura_ds.id)["pending"] == []


def test_consent_post_same_client_percent_encodes_the_code_into_its_invitation_url(
    client, peter, ow_app, oura_ds, peter_oura_pending
):
    code = mint_invitation_code(peter, ow_app)

    resp = client.post(f"/patient/consent/{oura_ds.id}/", {"code": code})

    assert resp.status_code == 302
    assert resp.url == f"http://localhost:8001/clients/ow/launch?code={quote(code, safe='')}"


def test_ow_launch_with_an_unconsented_code_goes_through_consent_first(
    client, peter, ow_app, oura_ds, peter_oura_pending
):
    code = mint_invitation_code(peter, ow_app)

    resp = client.get(f"/clients/ow/launch?code={code}")

    assert resp.status_code == 302
    assert resp.url == f"/patient/consent/{oura_ds.id}/?code={code}"


def test_ow_launch_with_a_consented_code_lists_the_consented_scopes(client, peter, ow_app):
    resp = client.get(f"/clients/ow/launch?code={mint_invitation_code(peter, ow_app)}")

    assert resp.status_code == 200
    assert (resp.context["source_name"], resp.context["source_labels"]) == ("Oura", "Sleep episode")


def test_non_patient_facing_source_is_invalid_on_consent_and_manage(client, peter, ehr_app):
    carex_ds = DataSource.objects.get(name="CareX")
    code = mint_invitation_code(peter, ehr_app)
    study_patient = StudyPatient.objects.get(study__name="Lifespan Study on Sleep & BP", patient=peter)
    omh_bp = CodeableConcept.objects.get(coding_code="omh:blood-pressure:4.0")

    assert client.get(f"/patient/consent/{carex_ds.id}/?code={code}").status_code == 400
    assert client.post(f"/patient/consent/{carex_ds.id}/", {"code": code}).status_code == 400
    assert not StudyPatientScopeConsent.objects.filter(study_patient=study_patient, scope_code=omh_bp).exists()

    StudyPatientScopeConsent.objects.create(
        study_patient=study_patient, scope_code=omh_bp, consented=True, consented_time=timezone.now()
    )
    assert client.get(f"/patient/manage/{carex_ds.id}/").status_code == 400


def test_manage_get_shows_the_consented_source(consented, ehr_ds, fhir_source):
    resp = consented.get(f"/patient/manage/{ehr_ds.id}/")

    assert resp.status_code == 200
    assert resp.context["rows"] == ["Clinical records"]
    assert resp.context["detail"] == f"{FACILITY} · Clinical records · 3 records"
    html = resp.content.decode()
    assert "You're sharing" in html and "Stop sharing" in html
    assert html.count("pf-card__icon") == 1  # one card for the source, not one per scope
    assert "pf-back" in html and 'href="/patient/"' in html


def test_manage_on_a_source_with_nothing_consented_redirects_to_the_hub(portal, ehr_ds):
    resp = portal.get(f"/patient/manage/{ehr_ds.id}/")
    assert (resp.status_code, resp.url) == (302, "/patient/")


def test_manage_and_done_share_the_per_type_sync_receipt(consented, ehr_ds, fhir_source):
    FhirAuxResource.objects.create(fhir_source=fhir_source, resource_type="Condition")

    for path in (f"/patient/manage/{ehr_ds.id}/", "/patient/done/"):
        resp = consented.get(path)
        assert resp.status_code == 200
        receipt = resp.context["receipt"]
        assert receipt["synced"] == [("Observations", 3), ("Conditions", 1)]  # by count desc, then label
        assert receipt["total"] == 4
        assert ("Devices", 0) in receipt["not_synced"]  # promised by the client's SMART scopes, nothing landed
        assert not any(label == "Observations" for label, _n in receipt["not_synced"])
        assert "Total synced" in resp.content.decode()


def test_manage_post_revokes_and_the_source_is_reconsentable(consented, pamela, ehr_ds, star):
    resp = consented.post(f"/patient/manage/{ehr_ds.id}/")
    assert (resp.status_code, resp.url) == (302, "/patient/")
    assert _star_consent(pamela, star).get().consented is False

    assert consented.get(f"/patient/consent/{ehr_ds.id}/").context["rows"] == ["Clinical records"]
    assert consented.post(f"/patient/consent/{ehr_ds.id}/").status_code == 302
    assert _star_consent(pamela, star).get().consented is True
    assert _card(consented.get("/patient/"), "EHR Patient Portal")["on"] is True


def test_revoking_oura_leaves_heart_rate_requested_via_carex_consented(portal, pamela, oura_ds, sleep_code):
    hr_code = CodeableConcept.objects.get(coding_code="omh:heart-rate:2.0")
    rows = StudyPatientScopeConsent.objects.filter(study_patient__patient=pamela)
    assert rows.filter(scope_code=hr_code, consented=True).exists()

    assert portal.get(f"/patient/manage/{oura_ds.id}/").context["rows"] == ["Sleep episode"]
    assert portal.post(f"/patient/manage/{oura_ds.id}/").status_code == 302

    assert not rows.filter(scope_code=sleep_code, consented=True).exists()
    assert rows.filter(scope_code=hr_code, consented=True).exists()  # BP & HR collects it via CareX, untouched


def test_manage_post_disconnects_oura_once_the_study_is_fully_unconsented(client, peter, ow_app, oura_ds):
    peter.jhe_user.identifier = "ow:abc"
    peter.jhe_user.save()
    ow_settings = {"ow.api_url": "https://ow.test", "ow.api_key": "k"}
    assert client.get(f"/patient/?code={mint_invitation_code(peter, ow_app)}").status_code == 200

    with (
        mock.patch("core.services.ow_ingest.get_setting", side_effect=lambda key, default="": ow_settings[key]),
        mock.patch("core.services.ow_ingest.requests.delete") as delete,
    ):
        delete.return_value.status_code = 204
        assert client.post(f"/patient/manage/{oura_ds.id}/").status_code == 302

    delete.assert_called_once()
    assert delete.call_args.args[0] == "https://ow.test/api/v1/users/abc/connections/oura"


def test_done_without_session_is_invalid(db, client):
    assert client.get("/patient/done/").status_code == 400


def test_done_shows_only_the_just_consented_source(consented, pamela, fhir_source):
    assert any(s["name"] == "Oura" and s["connected"] for s in _sources(pamela))  # a real other connected source

    resp = consented.get("/patient/done/")

    assert resp.status_code == 200
    assert resp.context["rows"] == [
        {"name": "EHR Patient Portal", "detail": f"{FACILITY} · Clinical records · 3 records"}
    ]
    assert resp.context["receipt"]["total"] == 3
    html = resp.content.decode()
    assert "You're all set" in html and "Oura" not in html
    assert "Manage sharing" in html and 'href="/patient/"' in html and "pf-back" in html


def test_done_leads_with_the_most_recently_consented_source_without_a_session_marker(client, peter, ehr_app, ehr_ds):
    assert client.get(f"/patient/?code={mint_invitation_code(peter, ehr_app)}").status_code == 200
    assert client.post(f"/patient/consent/{ehr_ds.id}/").status_code == 302
    session = client.session
    del session[SESSION_LAST_DS_KEY]
    session.save()

    resp = client.get("/patient/done/")

    assert resp.status_code == 200
    assert [row["name"] for row in resp.context["rows"]] == ["EHR Patient Portal"]  # not Peter's older Oura consent


def test_ehr_connect_with_an_unconsented_code_goes_through_consent_first(client, code, ehr_ds):
    resp = client.get(f"/clients/ehr-patient-portal/?code={code}")

    assert resp.status_code == 302
    assert resp.url == f"/patient/consent/{ehr_ds.id}/?code={code}"


def test_ehr_connect_with_a_consented_code_primes_the_session_for_the_done_page(client, pamela, star, code):
    study_patient = StudyPatient.objects.get(study__name="Lifespan Study on BP & HR", patient=pamela)
    StudyPatientScopeConsent.objects.create(
        study_patient=study_patient, scope_code=star, consented=True, consented_time=timezone.now()
    )

    assert client.get(f"/clients/ehr-patient-portal/?code={code}").status_code == 200
    resp = client.get("/patient/done/")

    assert resp.status_code == 200
    assert [row["name"] for row in resp.context["rows"]] == ["EHR Patient Portal"]


def test_consent_post_cross_client_to_clinical_records_lands_on_the_picker(client, peter, ow_app, ehr_app, ehr_ds):
    code = mint_invitation_code(peter, ow_app)

    resp = client.post(f"/patient/consent/{ehr_ds.id}/", {"code": code})

    assert resp.status_code == 302
    assert resp.url.startswith("/clients/ehr-patient-portal/?code=")
    assert code not in resp.url  # the EHR client's own freshly minted invitation, not the OW one
    assert PatientInvitation.objects.filter(
        patient=peter, client=ehr_app, status=PatientInvitation.Status.ISSUED
    ).exists()
