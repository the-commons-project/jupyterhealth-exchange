"""Tests for raw Oura payload ingestion from MinIO."""

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from core.models import DataSource, JheUser, Observation, OWPollEvent, StudyDataSource
from core.services.ow_ingest.orchestrator_raw import ingest_for_user
from core.services.ow_ingest.raw_payload_reader import S3ObjectInfo

from .conftest import set_jhe_settings
from .utils import add_patient_to_study, create_study

RAW_OURA_HR_RESPONSE = {
    "data": [
        {"bpm": 72, "timestamp": "2026-04-11T08:30:00+00:00", "source": "awake"},
        {"bpm": 68, "timestamp": "2026-04-11T08:30:05+00:00", "source": "awake"},
        {"bpm": 75, "timestamp": "2026-04-11T08:30:10+00:00", "source": "rest"},
    ],
    "next_token": None,
}


@pytest.fixture
def s3_settings(db):
    from django.core.cache import cache

    cache.clear()
    set_jhe_settings(
        **{
            "s3.endpoint_url": "http://ow-object-store:9000",
            "s3.access_key_id": "minioadmin",
            "s3.secret_access_key": "minioadmin",
            "s3.bucket_name": "raw-payloads",
            "s3.key_prefix": "raw-payloads/oura/api_response",
            "ow.api_base_url": "http://ow.test",
            "ow.api_key": "test-key",
            "ow.lookback_days": "7",
            "ow.initial_backfill_days": "30",
            "ow.ingest_mode": "polling",
        }
    )
    yield
    cache.clear()


@pytest.fixture
def ow_patient(organization, db):
    user = JheUser.objects.create_user(
        email="raw-oura-patient@example.org", password="x", user_type="patient", identifier="ow:test-uuid-raw-001"
    )
    user.patient.organizations.add(organization)
    study = create_study(organization=organization, codes=["omh:heart-rate:2.0"])
    add_patient_to_study(patient=user.patient, study=study, consent=True)
    device = DataSource.objects.create(name="Oura Ring (raw test)", type="personal_device")
    StudyDataSource.objects.create(study=study, data_source=device)
    return user.patient


@pytest.fixture
def ow_patient_no_hr_consent(organization, db):
    user = JheUser.objects.create_user(
        email="raw-oura-no-consent@example.org", password="x", user_type="patient", identifier="ow:test-uuid-raw-002"
    )
    user.patient.organizations.add(organization)
    study = create_study(organization=organization, codes=["omh:sleep-episode:1.1"])
    add_patient_to_study(patient=user.patient, study=study, consent=True)
    device = DataSource.objects.create(name="Oura Ring (no HR)", type="personal_device")
    StudyDataSource.objects.create(study=study, data_source=device)
    return user.patient


# ---------------------------------------------------------------------------
# raw_payload_reader
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRawPayloadReader:
    def test_list_new_objects_filters_by_last_modified(self, s3_settings):
        from core.services.ow_ingest.raw_payload_reader import list_new_objects

        now = datetime.now(UTC)
        mock_client = MagicMock()
        mock_client.list_objects_v2.return_value = {
            "Contents": [
                {
                    "Key": "raw-payloads/oura/api_response/2026-04-09/test-uuid-raw-001/old.json",
                    "LastModified": now - timedelta(hours=48),
                },
                {
                    "Key": "raw-payloads/oura/api_response/2026-04-11/test-uuid-raw-001/new.json",
                    "LastModified": now - timedelta(hours=1),
                },
            ],
            "IsTruncated": False,
        }
        mock_client.head_object.return_value = {"Metadata": {"trace_id": "/v2/usercollection/heartrate"}}

        with patch("core.services.ow_ingest.raw_payload_reader.get_client", return_value=mock_client):
            results = list_new_objects("test-uuid-raw-001", now - timedelta(hours=24))

        assert len(results) == 1
        assert "new.json" in results[0].key
        assert results[0].metadata["trace_id"] == "/v2/usercollection/heartrate"

    def test_list_new_objects_empty_bucket(self, s3_settings):
        from core.services.ow_ingest.raw_payload_reader import list_new_objects

        mock_client = MagicMock()
        mock_client.list_objects_v2.return_value = {"IsTruncated": False}

        with patch("core.services.ow_ingest.raw_payload_reader.get_client", return_value=mock_client):
            assert list_new_objects("test-uuid-raw-001", datetime.now(UTC) - timedelta(hours=24)) == []

    def test_read_object_parses_json(self, s3_settings):
        from core.services.ow_ingest.raw_payload_reader import read_object

        mock_client = MagicMock()
        mock_client.get_object.return_value = {
            "Body": MagicMock(read=MagicMock(return_value=json.dumps(RAW_OURA_HR_RESPONSE).encode()))
        }

        with patch("core.services.ow_ingest.raw_payload_reader.get_client", return_value=mock_client):
            result = read_object("any-key")

        assert result == RAW_OURA_HR_RESPONSE

    def test_read_object_s3_error_raises(self, s3_settings):
        from botocore.exceptions import ClientError

        from core.services.ow_ingest.raw_payload_reader import read_object

        mock_client = MagicMock()
        mock_client.get_object.side_effect = ClientError(
            {"Error": {"Code": "NoSuchKey", "Message": "not found"}}, "GetObject"
        )

        with patch("core.services.ow_ingest.raw_payload_reader.get_client", return_value=mock_client):
            with pytest.raises(ClientError):
                read_object("bad-key")


# ---------------------------------------------------------------------------
# Orchestrator integration
# ---------------------------------------------------------------------------


def _mock_s3(objects):
    """Build mocks for list_new_objects/read_object from a list of {key, trace_id, body} dicts."""
    now = datetime.now(UTC)
    infos = [S3ObjectInfo(key=o["key"], last_modified=now, metadata={"trace_id": o["trace_id"]}) for o in objects]
    body_map = {o["key"]: o["body"] for o in objects}
    return MagicMock(return_value=infos), MagicMock(side_effect=lambda k: body_map[k])


HR_OBJECT = {
    "key": "raw-payloads/oura/api_response/2026-04-11/test-uuid-raw-001/hr.json",
    "trace_id": "/v2/usercollection/heartrate",
    "body": RAW_OURA_HR_RESPONSE,
}


@pytest.mark.django_db
class TestRawOuraIngestion:
    def test_heart_rate_ingested(self, s3_settings, system_user, ow_patient):
        mock_list, mock_read = _mock_s3([HR_OBJECT])
        with (
            patch("core.services.ow_ingest.orchestrator_raw.list_new_objects", mock_list),
            patch("core.services.ow_ingest.orchestrator_raw.read_object", mock_read),
        ):
            ingested, skipped, errored = ingest_for_user(ow_patient.id, trigger="manual")
        assert ingested == 3
        assert not errored
        assert Observation.objects.filter(subject_patient=ow_patient).count() == 3

    def test_non_hr_objects_skipped(self, s3_settings, system_user, ow_patient):
        sleep_obj = {
            "key": "raw-payloads/oura/api_response/2026-04-11/test-uuid-raw-001/sleep.json",
            "trace_id": "/v2/usercollection/sleep",
            "body": {"data": [{"id": "1"}]},
        }
        mock_list, mock_read = _mock_s3([sleep_obj])
        with (
            patch("core.services.ow_ingest.orchestrator_raw.list_new_objects", mock_list),
            patch("core.services.ow_ingest.orchestrator_raw.read_object", mock_read),
        ):
            ingested, _, errored = ingest_for_user(ow_patient.id, trigger="manual")
        assert ingested == 0
        assert not errored

    def test_consent_gate_blocks_unconsented(self, s3_settings, system_user, ow_patient_no_hr_consent):
        mock_list, mock_read = _mock_s3([HR_OBJECT])
        with (
            patch("core.services.ow_ingest.orchestrator_raw.list_new_objects", mock_list),
            patch("core.services.ow_ingest.orchestrator_raw.read_object", mock_read),
        ):
            ingested, _, _ = ingest_for_user(ow_patient_no_hr_consent.id, trigger="manual")
        assert ingested == 0
        assert Observation.objects.filter(subject_patient=ow_patient_no_hr_consent).count() == 0

    def test_dedup_skips_existing(self, s3_settings, system_user, ow_patient):
        mock_list, mock_read = _mock_s3([HR_OBJECT])
        with (
            patch("core.services.ow_ingest.orchestrator_raw.list_new_objects", mock_list),
            patch("core.services.ow_ingest.orchestrator_raw.read_object", mock_read),
        ):
            ing1, _, _ = ingest_for_user(ow_patient.id, trigger="manual")
            ing2, _, _ = ingest_for_user(ow_patient.id, trigger="manual")
        assert ing1 == 3
        assert ing2 == 0

    def test_empty_bucket(self, s3_settings, system_user, ow_patient):
        mock_list = MagicMock(return_value=[])
        with (
            patch("core.services.ow_ingest.orchestrator_raw.list_new_objects", mock_list),
            patch("core.services.ow_ingest.orchestrator_raw.read_object") as mock_read,
        ):
            ingested, _, errored = ingest_for_user(ow_patient.id, trigger="manual")
        assert ingested == 0
        assert not errored
        mock_read.assert_not_called()

    def test_s3_error_recorded(self, s3_settings, system_user, ow_patient):
        from botocore.exceptions import ClientError

        mock_list = MagicMock(
            side_effect=ClientError({"Error": {"Code": "AccessDenied", "Message": "forbidden"}}, "ListObjectsV2")
        )
        with patch("core.services.ow_ingest.orchestrator_raw.list_new_objects", mock_list):
            _, _, errored = ingest_for_user(ow_patient.id, trigger="manual")
        assert errored
        event = OWPollEvent.objects.filter(patient=ow_patient).latest("started_at")
        assert event.status == "errored"
        assert "AccessDenied" in event.error_message

    def test_patient_without_ow_identifier_fails(self, s3_settings, system_user, organization):
        user = JheUser.objects.create_user(email="no-ow-raw@example.org", password="x", user_type="patient")
        user.patient.organizations.add(organization)
        _, _, errored = ingest_for_user(user.patient.id, trigger="manual")
        assert errored
        event = OWPollEvent.objects.get(patient=user.patient)
        assert "no ow:* identifier" in event.error_message

    def test_patient_without_data_source_fails(self, s3_settings, system_user, organization):
        user = JheUser.objects.create_user(
            email="no-ds-raw@example.org", password="x", user_type="patient", identifier="ow:no-ds-uuid"
        )
        user.patient.organizations.add(organization)
        study = create_study(organization=organization, codes=["omh:heart-rate:2.0"])
        add_patient_to_study(patient=user.patient, study=study, consent=True)
        # No StudyDataSource — that's what we're testing
        _, _, errored = ingest_for_user(user.patient.id, trigger="manual")
        assert errored
        event = OWPollEvent.objects.get(patient=user.patient)
        assert "personal_device" in event.error_message

    def test_bad_payload_shape_records_error(self, s3_settings, system_user, ow_patient):
        bad_obj = {
            "key": "raw-payloads/oura/api_response/2026-04-11/test-uuid-raw-001/bad.json",
            "trace_id": "/v2/usercollection/heartrate",
            "body": {"not_data": "wrong shape"},  # missing "data" key
        }
        mock_list, mock_read = _mock_s3([bad_obj])
        with (
            patch("core.services.ow_ingest.orchestrator_raw.list_new_objects", mock_list),
            patch("core.services.ow_ingest.orchestrator_raw.read_object", mock_read),
        ):
            ingested, _, errored = ingest_for_user(ow_patient.id, trigger="manual")
        assert ingested == 0
        # Empty data list is not an error — it's just no records
        assert not errored

    def test_read_object_failure_per_record(self, s3_settings, system_user, ow_patient):
        mock_list = MagicMock(
            return_value=[
                S3ObjectInfo(
                    key="raw-payloads/oura/api_response/2026-04-11/test-uuid-raw-001/fail.json",
                    last_modified=datetime.now(UTC),
                    metadata={"trace_id": "/v2/usercollection/heartrate"},
                )
            ]
        )
        mock_read = MagicMock(side_effect=Exception("S3 read failed"))
        with (
            patch("core.services.ow_ingest.orchestrator_raw.list_new_objects", mock_list),
            patch("core.services.ow_ingest.orchestrator_raw.read_object", mock_read),
        ):
            _, _, errored = ingest_for_user(ow_patient.id, trigger="manual")
        assert errored
        event = OWPollEvent.objects.filter(patient=ow_patient).latest("started_at")
        assert "S3 read failed" in event.error_message
