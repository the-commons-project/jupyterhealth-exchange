from django.core import mail
from oauth2_provider.models import get_application_model

from core.models import JheClient
from core.views.patient_invitation import _single_pending_study_name

from .utils import Code, add_patient_to_study, create_study

Application = get_application_model()


def test_create_invitation_returns_201(api_client, user, patient):
    client_app = Application.objects.create(
        name="invite client",
        user=user,
        client_type=Application.CLIENT_PUBLIC,
        authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
    )
    JheClient.objects.create(application=client_app, invitation_url="https://example.org/CODE")
    r = api_client.post(
        "/api/v1/invitation",
        {"patient_id": patient.id, "client_id": client_app.id, "send_email": False},
    )
    assert r.status_code == 201, r.text
    assert "invitationLink" in r.json()


def test_create_invitation_sends_a_branded_email(api_client, user, patient):
    client_app = Application.objects.create(
        name="invite client",
        user=user,
        client_type=Application.CLIENT_PUBLIC,
        authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
    )
    JheClient.objects.create(application=client_app, invitation_url="https://example.org/CODE")
    mail.outbox = []

    r = api_client.post(
        "/api/v1/invitation",
        {"patient_id": patient.id, "client_id": client_app.id, "send_email": True},
    )

    assert r.status_code == 201, r.text
    assert mail.outbox[0].subject == "You're invited to share your health data"
    assert "/static/common/images/jupyterhealth-logo.png" in mail.outbox[0].body


def test_single_pending_study_name_none_when_no_pending_studies(db, patient):
    # A freshly-created patient has no study enrollment, so no pending study to name.
    assert _single_pending_study_name(patient) is None


def test_single_pending_study_name_returns_name_of_sole_pending_study(organization, patient):
    study = create_study(name="Cardiometabolic Health Study", organization=organization, codes=[Code.HeartRate])
    add_patient_to_study(patient=patient, study=study, consent=False)
    assert _single_pending_study_name(patient) == "Cardiometabolic Health Study"


def test_single_pending_study_name_none_when_multiple_pending_studies(organization, patient):
    study_a = create_study(name="Study A", organization=organization, codes=[Code.HeartRate])
    study_b = create_study(name="Study B", organization=organization, codes=[Code.BloodPressure])
    add_patient_to_study(patient=patient, study=study_a, consent=False)
    add_patient_to_study(patient=patient, study=study_b, consent=False)
    assert _single_pending_study_name(patient) is None


# --- who may invite a patient ----------------------------------------------------------------


def _invite_as(who, patient, org_role=None, organization=None):
    """POST an invitation for `patient` as `who` (a user, optionally given a role in `organization`)."""
    from rest_framework.test import APIClient

    from core.models import PractitionerOrganization

    if org_role:
        PractitionerOrganization.objects.create(practitioner=who.practitioner, organization=organization, role=org_role)
    app = Application.objects.create(
        name=f"client for {who.email}",
        user=who,
        client_type=Application.CLIENT_PUBLIC,
        authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
    )
    JheClient.objects.create(application=app, invitation_url="https://example.org/CODE")
    client = APIClient()
    client.default_format = "json"
    client.force_authenticate(who)
    return client.post("/api/v1/invitation", {"patient_id": patient.id, "client_id": app.id})


def _practitioner(email):
    from core.models import JheUser

    return JheUser.objects.create_user(email=email, password="x", user_type="practitioner")


def test_a_member_of_the_patients_organization_can_invite(organization, patient):
    assert _invite_as(_practitioner("m@example.org"), patient, "member", organization).status_code == 201


def test_a_manager_of_the_patients_organization_can_invite(organization, patient):
    assert _invite_as(_practitioner("g@example.org"), patient, "manager", organization).status_code == 201


def test_a_superuser_can_invite_any_patient(superuser, patient):
    assert _invite_as(superuser, patient).status_code == 201


def test_a_viewer_cannot_invite(organization, patient):
    assert _invite_as(_practitioner("v@example.org"), patient, "viewer", organization).status_code == 403


def test_a_practitioner_of_another_organization_cannot_invite(organization, patient):
    from core.models import Organization

    other = Organization.objects.create(name="Other Org", type="other")
    assert _invite_as(_practitioner("o@example.org"), patient, "manager", other).status_code == 403


def test_a_practitioner_with_no_organization_cannot_invite(patient):
    assert _invite_as(_practitioner("n@example.org"), patient).status_code == 403


def test_a_patient_with_no_organization_can_be_invited_only_by_a_superuser(organization, user):
    from core.models import JheUser

    orphan = JheUser.objects.create_user(email="orphan@example.org", password="x", user_type="patient").patient
    assert _invite_as(user, orphan).status_code == 403  # a manager elsewhere, not of this patient
    superuser = JheUser.objects.create_superuser(email="su2@example.org", password="x")
    assert _invite_as(superuser, orphan).status_code == 201


def test_a_missing_patient_answers_like_a_forbidden_one(api_client):
    r = api_client.post("/api/v1/invitation", {"patient_id": 999999, "client_id": 1})
    assert r.status_code == 403


def test_a_refused_invitation_creates_nothing(organization, patient):
    from core.models import PatientInvitation

    _invite_as(_practitioner("v2@example.org"), patient, "viewer", organization)
    assert PatientInvitation.objects.count() == 0


def test_list_read_and_delete_reach_only_the_invitations_of_patients_the_caller_manages(organization, patient):
    from rest_framework.test import APIClient

    from core.models import Organization, PatientInvitation, PractitionerOrganization

    app = Application.objects.create(
        name="c",
        user=_practitioner("owner@example.org"),
        client_type=Application.CLIENT_PUBLIC,
        authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
    )
    invitation = PatientInvitation.issue(patient, app)

    def as_user(email, role=None, org=None):
        who = _practitioner(email)
        if role:
            PractitionerOrganization.objects.create(practitioner=who.practitioner, organization=org, role=role)
        client = APIClient()
        client.default_format = "json"
        client.force_authenticate(who)
        return client

    other = Organization.objects.create(name="Other Org", type="other")
    for outsider in (
        as_user("a@example.org"),
        as_user("b@example.org", "manager", other),
        as_user("c@example.org", "viewer", organization),
    ):
        assert outsider.get("/api/v1/invitation").json()["results"] == []
        assert outsider.get(f"/api/v1/invitation/{invitation.id}").status_code == 404
        assert outsider.delete(f"/api/v1/invitation/{invitation.id}").status_code == 404
    assert PatientInvitation.objects.filter(id=invitation.id).exists()

    member = as_user("d@example.org", "member", organization)
    assert [i["id"] for i in member.get("/api/v1/invitation").json()["results"]] == [invitation.id]
    assert member.get(f"/api/v1/invitation/{invitation.id}").status_code == 200
    assert member.patch(f"/api/v1/invitation/{invitation.id}", {"status": "issued"}).status_code == 405
    assert member.delete(f"/api/v1/invitation/{invitation.id}").status_code == 204
