import json
import sys
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.test import Client

from core.models import DataSource, Observation, ObservationIdentifier, PatientWearableConnection

OW_SETTINGS = {"ow.api_url": "http://ow:8001", "ow.api_key": "key"}
POLL_SETTINGS = {**OW_SETTINGS, "ow.ingest_mode": "normalized"}
S3_SETTINGS = {
    "ow.ingest_mode": "raw",
    "s3.endpoint_url": "localhost:9000",
    "s3.access_key_id": "ak",
    "s3.secret_access_key": "sk",
    "s3.bucket_name": "raw-payloads",
    "s3.use_ssl": False,
    "s3.key_prefix": "raw-payloads",
}


def _settings_lookup(settings):
    return lambda k, d="": settings.get(k, d)


def _mock_json_response(payload, status=200):
    r = MagicMock(status_code=status, json=lambda: payload)
    r.raise_for_status = MagicMock()
    return r


def _valid_omh_record(uuid_value, dt="2026-04-09T08:30:00Z"):
    return {
        "header": {
            "uuid": uuid_value,
            "schema_id": {"namespace": "omh", "name": "heart-rate", "version": "2.0"},
            "source_creation_date_time": dt,
            "modality": "sensed",
            "external_datasheets": [{"datasheet_type": "manufacturer", "datasheet_reference": "Oura Ring"}],
        },
        "body": {
            "heart_rate": {"value": 72.0, "unit": "beats/min"},
            "effective_time_frame": {"date_time": dt},
        },
    }


class _FakeS3Response:
    def __init__(self, body):
        self._body = body

    def read(self):
        return self._body

    def close(self):
        pass

    def release_conn(self):
        pass


@pytest.fixture
def oura_device(db):
    return DataSource.objects.create(name="Oura Ring", type="personal_device")


@pytest.fixture
def ow_connection(patient, hr_study):
    return PatientWearableConnection.objects.create(
        patient=patient, provider="oura", ow_user_id="ow-test-user-123", consented_scopes=["heart_rate"]
    )


@pytest.fixture
def ow_oauth_app(db):
    from oauth2_provider.models import get_application_model

    return get_application_model().objects.create(
        name="OW Consent",
        client_type="public",
        authorization_grant_type="authorization-code",
        redirect_uris="http://localhost:8000/ow/consent",
    )


@pytest.fixture
def ow_invitation_code(patient, hr_study, ow_oauth_app):
    from core.models import StudyClient

    StudyClient.objects.create(study=hr_study, client=ow_oauth_app)
    grant = patient.jhe_user.create_authorization_code(ow_oauth_app.id, "test-verifier")
    return f"localhost:8000~{ow_oauth_app.client_id}~{grant.code}~test-verifier"


class TestOWClient:
    @patch("core.ow_client.requests.post")
    @patch("core.ow_client.requests.get")
    @patch("core.ow_client.get_setting")
    def test_create_user_creates_when_not_found(self, mock_setting, mock_get, mock_post):
        mock_setting.side_effect = _settings_lookup(OW_SETTINGS)
        mock_get.return_value = _mock_json_response([])
        mock_post.return_value = MagicMock(status_code=201, json=lambda: {"id": "ow-123"})

        from core.ow_client import create_user

        assert create_user("patient@example.com") == "ow-123"

    @patch("core.ow_client.requests.post")
    @patch("core.ow_client.requests.get")
    @patch("core.ow_client.get_setting")
    def test_create_user_returns_existing_without_creating(self, mock_setting, mock_get, mock_post):
        mock_setting.side_effect = _settings_lookup(OW_SETTINGS)
        mock_get.return_value = _mock_json_response([{"id": "ow-existing"}])

        from core.ow_client import create_user

        assert create_user("patient@example.com") == "ow-existing"
        mock_post.assert_not_called()

    @patch("core.ow_client.requests.post")
    @patch("core.ow_client.requests.get")
    @patch("core.ow_client.get_setting")
    def test_create_user_raises_on_unexpected_status(self, mock_setting, mock_get, mock_post):
        mock_setting.side_effect = _settings_lookup(OW_SETTINGS)
        mock_get.return_value = _mock_json_response([])
        mock_post.return_value = MagicMock(status_code=200, raise_for_status=MagicMock())

        from core.ow_client import create_user

        with pytest.raises(RuntimeError):
            create_user("patient@example.com")

    @patch("core.ow_client.requests.get")
    @patch("core.ow_client.get_setting")
    def test_get_authorize_url_from_redirect(self, mock_setting, mock_get):
        mock_setting.side_effect = _settings_lookup(OW_SETTINGS)
        mock_get.return_value = MagicMock(status_code=302, headers={"Location": "https://oura.example/auth"})

        from core.ow_client import get_authorize_url

        assert get_authorize_url("oura", "ow-1", "http://cb") == "https://oura.example/auth"

    @patch("core.ow_client.requests.get")
    @patch("core.ow_client.get_setting")
    def test_get_authorize_url_raises_on_missing_field(self, mock_setting, mock_get):
        mock_setting.side_effect = _settings_lookup(OW_SETTINGS)
        mock_get.return_value = _mock_json_response({"unexpected": "field"})

        from core.ow_client import get_authorize_url

        with pytest.raises(RuntimeError):
            get_authorize_url("oura", "ow-1", "http://cb")

    @patch("core.ow_client.requests.delete")
    @patch("core.ow_client.get_setting")
    def test_revoke_treats_404_as_success(self, mock_setting, mock_delete):
        mock_setting.side_effect = _settings_lookup(OW_SETTINGS)
        mock_delete.return_value = MagicMock(status_code=404)

        from core.ow_client import revoke_connection

        revoke_connection("ow-1", "oura")

    @patch("core.ow_client.requests.get")
    @patch("core.ow_client.get_setting")
    def test_get_heart_rate_data(self, mock_setting, mock_get):
        mock_setting.side_effect = _settings_lookup(OW_SETTINGS)
        mock_get.return_value = _mock_json_response(
            [{"timestamp": "2026-04-09T08:30:00+00:00", "type": "heart_rate", "value": 72, "unit": "bpm"}]
        )

        from core.ow_client import get_heart_rate_data

        result = get_heart_rate_data("ow-123", "2026-04-09T00:00:00Z", "2026-04-10T00:00:00Z")
        assert len(result) == 1
        assert result[0]["value"] == 72


class TestConsentViews:
    def test_consent_page_requires_code(self, db):
        r = Client().get("/ow/consent")
        assert r.status_code == 200
        assert b"Invalid or expired invite link" in r.content

    def test_consent_page_rejects_bad_code(self, db):
        r = Client().get("/ow/consent?code=bad~code~here~now")
        assert r.status_code == 200
        assert b"Invalid or expired invite link" in r.content

    def test_consent_page_rejects_malformed_code(self, db):
        r = Client().get("/ow/consent?code=only-one-part")
        assert r.status_code == 200
        assert b"Invalid or expired invite link" in r.content

    def test_consent_page_renders_for_patient(self, patient, hr_study, ow_invitation_code):
        r = Client().get(f"/ow/consent?code={ow_invitation_code}")
        assert r.status_code == 200
        assert b"Connect Your Wearable" in r.content

    def test_consent_page_shows_connected_state(self, patient, hr_study, ow_invitation_code, ow_connection):
        r = Client().get(f"/ow/consent?code={ow_invitation_code}")
        assert r.status_code == 200
        assert b"Wearable Connected" in r.content

    @patch("core.views.ow.ow_client")
    def test_consent_happy_path_creates_connection_and_redirects_to_authorize(
        self, mock_ow_client, patient, hr_study, ow_invitation_code
    ):
        mock_ow_client.create_user.return_value = "ow-user-abc"
        mock_ow_client.get_authorize_url.return_value = "https://oura.example/oauth/authorize?foo=bar"

        r = Client().post("/ow/consent", {"code": ow_invitation_code, "scopes": ["heart_rate"]})

        assert r.status_code == 302
        assert r["Location"] == "https://oura.example/oauth/authorize?foo=bar"
        conn = PatientWearableConnection.objects.get(patient=patient, provider="oura")
        assert conn.ow_user_id == "ow-user-abc"
        assert conn.consented_scopes == ["heart_rate"]
        mock_ow_client.create_user.assert_called_once_with(patient.jhe_user.email)

    @patch("core.views.ow.ow_client")
    def test_consent_rejects_invalid_scopes(self, mock_ow_client, patient, hr_study, ow_invitation_code):
        r = Client().post("/ow/consent", {"code": ow_invitation_code, "scopes": ["malicious_scope"]})
        assert r.status_code == 200
        assert b"select at least one data scope" in r.content
        assert not PatientWearableConnection.objects.filter(patient=patient).exists()
        mock_ow_client.create_user.assert_not_called()

    @patch("core.views.ow.ow_client.revoke_connection")
    def test_revoke_calls_ow_and_deletes_connection(
        self, mock_revoke, patient, hr_study, ow_invitation_code, ow_connection
    ):
        r = Client().post("/ow/consent", {"action": "revoke", "code": ow_invitation_code})
        assert r.status_code == 302
        assert PatientWearableConnection.objects.filter(patient=patient).count() == 0
        mock_revoke.assert_called_once_with(ow_connection.ow_user_id, "oura")

    @patch("core.views.ow.ow_client.revoke_connection")
    def test_revoke_preserves_connection_on_ow_failure(
        self, mock_revoke, patient, hr_study, ow_invitation_code, ow_connection
    ):
        mock_revoke.side_effect = Exception("OW is down")
        r = Client().post("/ow/consent", {"action": "revoke", "code": ow_invitation_code})
        assert r.status_code == 200
        assert b"Failed to revoke with Open Wearables" in r.content
        assert PatientWearableConnection.objects.filter(patient=patient).count() == 1

    def test_success_page_renders(self, db):
        r = Client().get("/ow/success")
        assert r.status_code == 200
        assert b"Success" in r.content

    def test_success_page_revoked_message(self, db):
        r = Client().get("/ow/success?action=revoked")
        assert r.status_code == 200
        assert b"consent has been revoked" in r.content

    def test_oauth_callback_rejects_unknown_user(self, db):
        r = Client().get("/ow/callback?ow_user_id=unknown-user-xxx")
        assert r.status_code == 200
        assert b"Unknown OAuth callback" in r.content

    def test_oauth_callback_redirects_for_known_user(self, ow_connection):
        r = Client().get(f"/ow/callback?ow_user_id={ow_connection.ow_user_id}")
        assert r.status_code == 302
        assert "/ow/success" in r["Location"]


class TestOWPollCommand:
    @pytest.fixture(autouse=True)
    def mock_omh_shim(self, monkeypatch):
        self.mock_omh = MagicMock()
        monkeypatch.setitem(sys.modules, "omh_shim", self.mock_omh)

    @pytest.fixture
    def mock_normalized_api(self, monkeypatch):
        """Mock ow_client + command-level get_setting with normalized poll settings."""
        monkeypatch.setattr("core.ow_client.get_setting", _settings_lookup(POLL_SETTINGS))
        monkeypatch.setattr("core.management.commands.ow_poll.get_setting", _settings_lookup(POLL_SETTINGS))
        get_mock = MagicMock(
            return_value=_mock_json_response([{"timestamp": "2026-04-09T08:30:00+00:00", "value": 72}])
        )
        monkeypatch.setattr("core.ow_client.requests.get", get_mock)
        return get_mock

    def test_normalized_poll_creates_observations(
        self, mock_normalized_api, patient, hr_study, oura_device, ow_connection
    ):
        self.mock_omh.convert.return_value = _valid_omh_record("test-uuid-123")
        call_command("ow_poll")
        assert ObservationIdentifier.objects.filter(system="omh-shim", value="test-uuid-123").exists()
        ow_connection.refresh_from_db()
        assert ow_connection.last_polled_at is not None

    def test_poll_skips_patient_without_study_scope_consent(self, mock_normalized_api, patient, oura_device):
        # Blocker #2 regression: patient has a PatientWearableConnection but no
        # StudyPatientScopeConsent — must not ingest.
        from core.models import CodeableConcept

        PatientWearableConnection.objects.create(
            patient=patient, provider="oura", ow_user_id="ow-unconsented", consented_scopes=["heart_rate"]
        )
        CodeableConcept.objects.get_or_create(
            coding_system="https://w3id.org/openmhealth",
            coding_code="omh:heart-rate:2.0",
            defaults={"text": "Heart Rate"},
        )
        self.mock_omh.convert.return_value = _valid_omh_record("should-not-create")

        call_command("ow_poll")

        assert not ObservationIdentifier.objects.filter(system="omh-shim", value="should-not-create").exists()
        assert Observation.objects.filter(subject_patient=patient).count() == 0

    def test_normalized_poll_skips_duplicates(self, mock_normalized_api, patient, hr_study, oura_device, ow_connection):
        self.mock_omh.convert.return_value = _valid_omh_record("dupe-uuid")
        call_command("ow_poll")
        call_command("ow_poll")
        assert ObservationIdentifier.objects.filter(system="omh-shim", value="dupe-uuid").count() == 1

    def test_poll_with_no_connections(self, db, oura_device):
        call_command("ow_poll")  # should not raise

    def test_poll_patient_id_flag_limits_scope(
        self, mock_normalized_api, patient, hr_study, oura_device, ow_connection
    ):
        from django.utils import timezone as tz

        from core.models import CodeableConcept, JheUser, StudyPatient, StudyPatientScopeConsent

        other_user = JheUser.objects.create_user(email="other-patient@example.org", user_type="patient")
        other_patient = other_user.patient
        other_patient.organizations.add(hr_study.organization)
        sp = StudyPatient.objects.create(study=hr_study, patient=other_patient)
        hr_code = CodeableConcept.objects.get(coding_code="omh:heart-rate:2.0")
        StudyPatientScopeConsent.objects.create(
            study_patient=sp, scope_code=hr_code, consented=True, consented_time=tz.now()
        )
        PatientWearableConnection.objects.create(
            patient=other_patient, provider="oura", ow_user_id="ow-other", consented_scopes=["heart_rate"]
        )

        call_command("ow_poll", patient_id=patient.id)

        called_urls = [c.args[0] for c in mock_normalized_api.call_args_list]
        assert any(ow_connection.ow_user_id in url for url in called_urls)
        assert not any("ow-other" in url for url in called_urls)

    @patch("core.management.commands.ow_poll.Minio")
    @patch("core.management.commands.ow_poll.get_setting")
    def test_raw_poll_reads_from_minio_and_filters_by_trace_id(
        self, mock_setting, mock_minio_cls, patient, hr_study, oura_device, ow_connection
    ):
        mock_setting.side_effect = _settings_lookup(S3_SETTINGS)

        hr_obj = MagicMock(object_name="raw-payloads/oura/api_response/2026-04-13/ow-test-user-123/abc.json")
        workout_obj = MagicMock(object_name="raw-payloads/oura/api_response/2026-04-13/ow-test-user-123/def.json")
        other_user_obj = MagicMock(object_name="raw-payloads/oura/api_response/2026-04-13/other-user/xyz.json")
        traces = {
            hr_obj.object_name: "/v2/usercollection/heartrate",
            workout_obj.object_name: "/v2/usercollection/workout",
        }
        client = MagicMock()
        client.list_objects.return_value = [hr_obj, workout_obj, other_user_obj]
        client.stat_object.side_effect = lambda bucket, key: MagicMock(metadata={"x-amz-meta-trace_id": traces[key]})
        client.get_object.return_value = _FakeS3Response(
            json.dumps({"data": [{"bpm": 72, "timestamp": "2026-04-09T08:30:00+00:00"}]}).encode()
        )
        mock_minio_cls.return_value = client
        self.mock_omh.convert.return_value = _valid_omh_record("raw-uuid-1")

        call_command("ow_poll")

        assert client.stat_object.call_count == 2
        client.get_object.assert_called_once_with("raw-payloads", hr_obj.object_name)
        assert ObservationIdentifier.objects.filter(system="omh-shim", value="raw-uuid-1").count() == 1

    @patch("core.management.commands.ow_poll.Minio")
    @patch("core.management.commands.ow_poll.get_setting")
    def test_raw_poll_skips_malformed_s3_payloads(
        self, mock_setting, mock_minio_cls, patient, hr_study, oura_device, ow_connection
    ):
        mock_setting.side_effect = _settings_lookup(S3_SETTINGS)
        bad_obj = MagicMock(object_name="raw-payloads/oura/api_response/2026-04-13/ow-test-user-123/bad.json")
        client = MagicMock()
        client.list_objects.return_value = [bad_obj]
        client.stat_object.return_value = MagicMock(metadata={"x-amz-meta-trace_id": "/v2/usercollection/heartrate"})
        # JSON string, not a dict — would previously crash .get()
        client.get_object.return_value = _FakeS3Response(json.dumps("not a dict").encode())
        mock_minio_cls.return_value = client

        call_command("ow_poll")  # must not raise

        assert Observation.objects.filter(subject_patient=patient).count() == 0
        ow_connection.refresh_from_db()
        assert ow_connection.last_polled_at is not None
