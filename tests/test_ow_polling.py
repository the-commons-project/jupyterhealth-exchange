"""Tests for the OW polling pipeline."""

import hashlib
import hmac
import json
import time
from unittest.mock import Mock, patch

import pytest
import requests
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from rest_framework.test import APIClient

from core.models import (
    DataSource,
    JheSetting,
    JheUser,
    Observation,
    OWPollEvent,
    OWPollStatus,
    Patient,
    StudyDataSource,
)
from core.services.ow_ingest import orchestrator_normalized as orch
from core.services.ow_ingest._common import build_polling_set
from core.services.ow_ingest.orchestrator_normalized import ingest_for_user
from core.services.ow_integration import load_and_validate_polling_config

from .conftest import set_jhe_settings
from .utils import add_patient_to_study, create_study

# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------


@pytest.fixture
def polling_settings(db):
    """Configure JheSetting keys the polling code reads."""
    from django.core.cache import cache

    cache.clear()
    set_jhe_settings(
        **{
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
def webhook_settings(polling_settings):
    """polling_settings plus ow.ingest_mode=webhook + a webhook secret."""
    set_jhe_settings(**{"ow.ingest_mode": "webhook", "ow.webhook_secret": "test-secret"})
    return "test-secret"


@pytest.fixture
def ow_patient(organization, db):
    """A patient with an ow:* identifier and active consent on a study with
    all 7 OMH codes plus a personal_device DataSource attached to the study.
    """
    user = JheUser.objects.create_user(
        email="ow-patient@example.org",
        password="x",
        user_type="patient",
        identifier="ow:test-uuid-12345",
    )
    user.patient.organizations.add(organization)

    study = create_study(
        organization=organization,
        codes=[
            "omh:heart-rate:2.0",
            "omh:heart-rate-variability:1.0",
            "omh:oxygen-saturation:2.0",
            "omh:step-count:3.0",
            "omh:sleep-duration:2.0",
            "omh:sleep-episode:1.1",
            "omh:physical-activity:1.2",
        ],
    )
    add_patient_to_study(patient=user.patient, study=study, consent=True)
    device = DataSource.objects.create(name="Oura Ring (test)", type="personal_device")
    StudyDataSource.objects.create(study=study, data_source=device)
    return user.patient


# Canned OW responses, one per data_type. Each carries a unique timestamp so
# tests can assert which sample produced which Observation.
CANNED_OW = {
    "heart_rate": {
        "timestamp": "2026-04-09T08:30:00+00:00",
        "type": "heart_rate",
        "value": 72,
        "unit": "bpm",
    },
    "heart_rate_variability": {
        "timestamp": "2026-04-09T03:15:00+00:00",
        "type": "heart_rate_variability",
        "value": 42.5,
        "unit": "ms",
    },
    "oxygen_saturation": {
        "timestamp": "2026-04-09T04:00:00+00:00",
        "type": "oxygen_saturation",
        "value": 97.5,
        "unit": "%",
    },
    "step_count": {"date": "2026-04-09", "steps": 8432},
    "sleep_duration": {"date": "2026-04-09", "sleep_total_duration_minutes": 432},
    "sleep_episode": {
        "record_id": "abc",
        "bedtime_start": "2026-04-09T22:30:00+00:00",
        "bedtime_end": "2026-04-10T06:45:00+00:00",
        "sleep_total_duration_minutes": 460,
    },
    "physical_activity": {
        "date": "2026-04-09",
        "active_calories_kcal": 342,
        "distance_meters": 6240.5,
    },
}


def _wire_canned_ow(mock_ow):
    """Side-effect the mock with the canned payloads."""
    mock_ow.fetch_timeseries.side_effect = lambda u, types, s, e: [CANNED_OW[t] for t in types if t in CANNED_OW]
    mock_ow.fetch_summaries.side_effect = lambda u, s, e: [
        {
            **CANNED_OW["step_count"],
            **CANNED_OW["sleep_duration"],
            **CANNED_OW["physical_activity"],
        }
    ]
    mock_ow.fetch_sleep_details.side_effect = lambda u, s, e: [CANNED_OW["sleep_episode"]]


# ---------------------------------------------------------------------------
# 1. ow_poll happy path — content-aware assertions
# ---------------------------------------------------------------------------


class TestOWPollHappyPath:
    @patch("core.services.ow_ingest.orchestrator_normalized.ow_service")
    def test_creates_one_observation_per_data_type_with_correct_codes(
        self, mock_ow, ow_patient, polling_settings, system_user
    ):
        """Every expected coding_code present, no duplicates."""
        _wire_canned_ow(mock_ow)
        call_command("ow_poll", f"--patient-id={ow_patient.id}")

        actual_codes = set(
            Observation.objects.filter(subject_patient=ow_patient).values_list(
                "codeable_concept__coding_code", flat=True
            )
        )
        assert actual_codes == {
            "omh:heart-rate:2.0",
            "omh:heart-rate-variability:1.0",
            "omh:oxygen-saturation:2.0",
            "omh:step-count:3.0",
            "omh:sleep-duration:2.0",
            "omh:sleep-episode:1.1",
            "omh:physical-activity:1.2",
        }
        assert Observation.objects.filter(subject_patient=ow_patient).count() == 7

    @patch("core.services.ow_ingest.orchestrator_normalized.ow_service")
    def test_every_observation_has_envelope_and_correct_schema_id(
        self, mock_ow, ow_patient, polling_settings, system_user
    ):
        """Envelope schema_id matches codeable_concept for all observations."""
        _wire_canned_ow(mock_ow)
        call_command("ow_poll", f"--patient-id={ow_patient.id}")

        for obs in Observation.objects.filter(subject_patient=ow_patient):
            data = obs.value_attachment_data
            assert isinstance(data, dict), "JSONB must be parsed dict, not raw string"
            assert "header" in data and "body" in data, "must have data-point envelope"
            schema_id = data["header"]["schema_id"]
            # Verify schema_id name and version match the coding_code.
            # HRV uses "local:" namespace in omh-shim but "omh:" in the
            # CodeableConcept, so we only check name + version.
            _, name, version = obs.codeable_concept.coding_code.split(":")
            assert schema_id["name"] == name
            assert schema_id["version"] == version
            # IEEE 1752.1 header: modality must be "sensed"
            assert data["header"]["modality"] == "sensed"

    @patch("core.services.ow_ingest.orchestrator_normalized.ow_service")
    def test_heart_rate_body_has_correct_value_and_timestamp(self, mock_ow, ow_patient, polling_settings, system_user):
        """Heart rate body has correct value and timestamp."""
        _wire_canned_ow(mock_ow)
        call_command("ow_poll", f"--patient-id={ow_patient.id}")

        hr = Observation.objects.get(
            subject_patient=ow_patient,
            codeable_concept__coding_code="omh:heart-rate:2.0",
        )
        body = hr.value_attachment_data["body"]
        assert body["heart_rate"] == {"value": 72, "unit": "beats/min"}
        assert body["effective_time_frame"]["date_time"] == "2026-04-09T08:30:00Z"

    @patch("core.services.ow_ingest.orchestrator_normalized.ow_service")
    def test_sleep_episode_body_has_correct_time_interval(self, mock_ow, ow_patient, polling_settings, system_user):
        """Sleep episode uses time_interval, not date_time."""
        _wire_canned_ow(mock_ow)
        call_command("ow_poll", f"--patient-id={ow_patient.id}")

        se = Observation.objects.get(
            subject_patient=ow_patient,
            codeable_concept__coding_code="omh:sleep-episode:1.1",
        )
        ti = se.value_attachment_data["body"]["effective_time_frame"]["time_interval"]
        assert ti["start_date_time"] == "2026-04-09T22:30:00Z"
        assert ti["end_date_time"] == "2026-04-10T06:45:00Z"

    @patch("core.services.ow_ingest.orchestrator_normalized.ow_service")
    def test_step_count_uses_canonical_unit_value_shape(self, mock_ow, ow_patient, polling_settings, system_user):
        """Step count uses canonical OMH unit_value shape."""
        _wire_canned_ow(mock_ow)
        call_command("ow_poll", f"--patient-id={ow_patient.id}")

        sc = Observation.objects.get(
            subject_patient=ow_patient,
            codeable_concept__coding_code="omh:step-count:3.0",
        )
        body = sc.value_attachment_data["body"]
        assert body["step_count"] == {"value": 8432, "unit": "steps"}

    @patch("core.services.ow_ingest.orchestrator_normalized.ow_service")
    def test_poll_status_rows_per_data_type(self, mock_ow, ow_patient, polling_settings, system_user):
        _wire_canned_ow(mock_ow)
        call_command("ow_poll", f"--patient-id={ow_patient.id}")

        statuses = OWPollStatus.objects.filter(patient=ow_patient)
        assert statuses.count() == 7
        assert set(statuses.values_list("data_type", flat=True)) == set(orch.DATA_TYPES)
        for s in statuses:
            assert s.backfill_complete is True, f"{s.data_type} should be marked complete"
            assert s.last_error is None
            assert s.last_success_at is not None
            assert s.last_poll_at is not None
            assert s.disabled is False

    @patch("core.services.ow_ingest.orchestrator_normalized.ow_service")
    def test_owpollevent_records_correct_counters(self, mock_ow, ow_patient, polling_settings, system_user):
        _wire_canned_ow(mock_ow)
        call_command("ow_poll", f"--patient-id={ow_patient.id}")

        event = OWPollEvent.objects.get(patient=ow_patient)
        assert event.status == "completed"
        assert event.records_ingested == 7
        assert event.records_skipped == 0
        assert event.error_message is None
        assert event.trigger == "manual"
        assert event.completed_at is not None
        assert event.started_at <= event.completed_at

    @patch("core.services.ow_ingest.orchestrator_normalized.ow_service")
    def test_dedup_on_second_run_makes_zero_new_observations(self, mock_ow, ow_patient, polling_settings, system_user):
        _wire_canned_ow(mock_ow)
        call_command("ow_poll", f"--patient-id={ow_patient.id}")
        # Reset the OW call counts so we can prove the second run still
        # called OW (which proves dedup is the reason for 0 new obs, not
        # an early exit).
        mock_ow.fetch_summaries.reset_mock()
        mock_ow.fetch_timeseries.reset_mock()
        mock_ow.fetch_sleep_details.reset_mock()
        call_command("ow_poll", f"--patient-id={ow_patient.id}")

        assert Observation.objects.filter(subject_patient=ow_patient).count() == 7
        assert mock_ow.fetch_summaries.call_count == 1
        assert mock_ow.fetch_timeseries.call_count == 1
        assert mock_ow.fetch_sleep_details.call_count == 1
        events = list(OWPollEvent.objects.filter(patient=ow_patient).order_by("started_at"))
        assert len(events) == 2
        assert events[0].records_ingested == 7
        assert events[1].records_ingested == 0

    @patch("core.services.ow_ingest.orchestrator_normalized.ow_service")
    def test_dry_run_with_http_error_does_not_create_owpollstatus(
        self, mock_ow, ow_patient, polling_settings, system_user
    ):
        """Dry-run with HTTP error creates no OWPollStatus rows."""
        mock_ow.fetch_timeseries.side_effect = requests.ConnectionError("ow down")
        mock_ow.fetch_summaries.side_effect = requests.ConnectionError("ow down")
        mock_ow.fetch_sleep_details.side_effect = requests.ConnectionError("ow down")

        call_command("ow_poll", f"--patient-id={ow_patient.id}", "--dry-run")

        assert OWPollStatus.objects.filter(patient=ow_patient).count() == 0
        event = OWPollEvent.objects.get(patient=ow_patient)
        assert event.status == "errored"
        assert "ow down" in event.error_message

    @patch("core.services.ow_ingest.orchestrator_normalized.ow_service")
    def test_dry_run_writes_no_state(self, mock_ow, ow_patient, polling_settings, system_user):
        """Dry-run creates no Observations or OWPollStatus but still calls OW."""
        _wire_canned_ow(mock_ow)
        call_command("ow_poll", f"--patient-id={ow_patient.id}", "--dry-run")

        assert Observation.objects.filter(subject_patient=ow_patient).count() == 0
        # Dry-run is purely read-only on OWPollStatus — no rows created.
        assert OWPollStatus.objects.filter(patient=ow_patient).count() == 0
        # OWPollEvent IS created — that's the audit trail and matches the
        # operator expectation that they can verify the dry-run ran.
        event = OWPollEvent.objects.get(patient=ow_patient, trigger="manual")
        assert event.records_ingested == 7  # dry-run still counts what it would do
        # OW was actually called.
        assert mock_ow.fetch_summaries.call_count == 1
        assert mock_ow.fetch_timeseries.call_count == 1
        assert mock_ow.fetch_sleep_details.call_count == 1


# ---------------------------------------------------------------------------
# 2. Failure modes — fail-fast at HTTP, fail-soft at record level
# ---------------------------------------------------------------------------


class TestOWPollFailures:
    @patch("core.services.ow_ingest.orchestrator_normalized.ow_service")
    def test_connection_error_records_last_error_on_all_data_types(
        self, mock_ow, ow_patient, polling_settings, system_user
    ):
        mock_ow.fetch_timeseries.side_effect = requests.ConnectionError("ow down")
        mock_ow.fetch_summaries.side_effect = requests.ConnectionError("ow down")
        mock_ow.fetch_sleep_details.side_effect = requests.ConnectionError("ow down")

        call_command("ow_poll", f"--patient-id={ow_patient.id}")

        assert Observation.objects.filter(subject_patient=ow_patient).count() == 0
        statuses = OWPollStatus.objects.filter(patient=ow_patient)
        for s in statuses:
            assert "ow down" in s.last_error
            assert s.backfill_complete is False
            assert s.last_success_at is None
        event = OWPollEvent.objects.get(patient=ow_patient)
        assert event.status == "errored"
        assert event.records_ingested == 0
        assert event.records_skipped == 0
        assert event.error_message  # not None and not empty

    @patch("core.services.ow_ingest.orchestrator_normalized.ow_service")
    def test_http_500_aborts_per_data_type_not_per_patient(self, mock_ow, ow_patient, polling_settings, system_user):
        """HTTP 500 on /summaries fails summary types but timeseries + sleep succeed."""
        resp_500 = Mock()
        resp_500.status_code = 500
        mock_ow.fetch_summaries.side_effect = requests.HTTPError("server error", response=resp_500)
        # /timeseries and /sleep work normally
        mock_ow.fetch_timeseries.side_effect = lambda u, types, s, e: [CANNED_OW[t] for t in types if t in CANNED_OW]
        mock_ow.fetch_sleep_details.side_effect = lambda u, s, e: [CANNED_OW["sleep_episode"]]

        call_command("ow_poll", f"--patient-id={ow_patient.id}")

        # The 3 summary data types failed
        for dt in ("step_count", "sleep_duration", "physical_activity"):
            s = OWPollStatus.objects.get(patient=ow_patient, data_type=dt)
            assert "OW fetch failed" in s.last_error
            assert s.backfill_complete is False
        # The 4 non-summary data types succeeded (HR, HRV, O2, sleep_episode)
        for dt in ("heart_rate", "heart_rate_variability", "oxygen_saturation", "sleep_episode"):
            s = OWPollStatus.objects.get(patient=ow_patient, data_type=dt)
            assert s.last_error is None
            assert s.backfill_complete is True
        # 4 observations got ingested (HR, HRV, O2, sleep_episode)
        assert Observation.objects.filter(subject_patient=ow_patient).count() == 4
        event = OWPollEvent.objects.get(patient=ow_patient)
        assert event.status == "errored"  # any data_type error → errored
        assert event.records_ingested == 4
        assert event.records_skipped == 0

    @patch("core.services.ow_ingest.orchestrator_normalized.ow_service")
    def test_http_401_aborts_data_type_records_error(self, mock_ow, ow_patient, polling_settings, system_user):
        resp_401 = Mock()
        resp_401.status_code = 401
        err = requests.HTTPError("unauthorized", response=resp_401)
        mock_ow.fetch_timeseries.side_effect = err
        mock_ow.fetch_summaries.side_effect = err
        mock_ow.fetch_sleep_details.side_effect = err

        call_command("ow_poll", f"--patient-id={ow_patient.id}")

        for s in OWPollStatus.objects.filter(patient=ow_patient):
            assert s.last_error is not None
            assert s.backfill_complete is False

    @patch("core.services.ow_ingest.orchestrator_normalized.ow_service")
    def test_unexpected_response_shape_recorded_as_error_not_crash(
        self, mock_ow, ow_patient, polling_settings, system_user
    ):
        """Garbage OW response shape recorded as error, not a crash."""
        mock_ow.fetch_timeseries.side_effect = ValueError("unexpected shape: got str")
        mock_ow.fetch_summaries.side_effect = ValueError("unexpected shape: got str")
        mock_ow.fetch_sleep_details.side_effect = ValueError("unexpected shape: got str")

        call_command("ow_poll", f"--patient-id={ow_patient.id}")

        for s in OWPollStatus.objects.filter(patient=ow_patient):
            assert "unexpected shape" in s.last_error
            assert s.backfill_complete is False
        assert Observation.objects.filter(subject_patient=ow_patient).count() == 0
        event = OWPollEvent.objects.get(patient=ow_patient)
        assert event.status == "errored"

    @patch("core.services.ow_ingest.orchestrator_normalized.ow_service")
    def test_http_404_treated_as_no_data_not_an_error(self, mock_ow, ow_patient, polling_settings, system_user):
        """404 = no data, not an error. last_error stays None."""
        resp_404 = Mock()
        resp_404.status_code = 404
        err = requests.HTTPError("not found", response=resp_404)
        mock_ow.fetch_timeseries.side_effect = err
        mock_ow.fetch_summaries.side_effect = err
        mock_ow.fetch_sleep_details.side_effect = err

        call_command("ow_poll", f"--patient-id={ow_patient.id}")

        assert Observation.objects.filter(subject_patient=ow_patient).count() == 0
        for s in OWPollStatus.objects.filter(patient=ow_patient):
            assert s.last_error is None
            assert s.last_success_at is not None
            # Empty response means we keep backfill_complete=False so the
            # next poll still uses the wide window.
            assert s.backfill_complete is False
        event = OWPollEvent.objects.get(patient=ow_patient)
        assert event.status == "completed"

    @patch("core.services.ow_ingest.orchestrator_normalized.omh_shim.convert")
    @patch("core.services.ow_ingest.orchestrator_normalized.ow_service")
    def test_record_level_conversion_error_skips_exact_count(
        self, mock_ow, mock_convert, ow_patient, polling_settings, system_user
    ):
        """All 7 samples fail conversion → exact skip count."""
        _wire_canned_ow(mock_ow)
        mock_convert.side_effect = ValueError("bad sample")
        call_command("ow_poll", f"--patient-id={ow_patient.id}")

        assert Observation.objects.filter(subject_patient=ow_patient).count() == 0
        for s in OWPollStatus.objects.filter(patient=ow_patient):
            assert s.last_error is None  # record-level, not HTTP
            assert s.last_success_at is not None
        event = OWPollEvent.objects.get(patient=ow_patient)
        assert event.records_skipped == 7  # exact, not > 0
        assert event.records_ingested == 0
        assert event.status == "completed"  # record errors aren't HTTP errors

    @patch("core.services.ow_ingest.orchestrator_normalized.Observation.fhir_create")
    @patch("core.services.ow_ingest.orchestrator_normalized.ow_service")
    def test_fhir_create_failure_skips_record_continues_loop(
        self, mock_ow, mock_fhir_create, ow_patient, polling_settings, system_user
    ):
        """fhir_create failure skips record, continues to next."""
        _wire_canned_ow(mock_ow)
        mock_fhir_create.side_effect = ValueError("ingest broken")

        call_command("ow_poll", f"--patient-id={ow_patient.id}")

        # 7 samples in, 7 fhir_create failures → 7 skipped, 0 ingested.
        event = OWPollEvent.objects.get(patient=ow_patient)
        assert event.records_skipped == 7
        assert event.records_ingested == 0
        # Each data type still recorded a clean (non-HTTP-level) success.
        for s in OWPollStatus.objects.filter(patient=ow_patient):
            assert s.last_error is None
            assert s.last_success_at is not None
        # Real Observation table is empty.
        assert Observation.objects.filter(subject_patient=ow_patient).count() == 0

    def test_patient_without_consent_not_in_polling_set(self, organization):
        user = JheUser.objects.create_user(
            email="no-consent@example.org",
            password="x",
            user_type="patient",
            identifier="ow:no-consent",
        )
        user.patient.organizations.add(organization)
        assert user.patient not in build_polling_set()

    def test_patient_without_ow_identifier_not_in_polling_set(self, organization):
        user = JheUser.objects.create_user(
            email="no-ow@example.org",
            password="x",
            user_type="patient",
        )
        user.patient.organizations.add(organization)
        assert user.patient not in build_polling_set()

    def test_ingest_for_user_on_patient_without_ow_identifier_records_event(
        self, organization, polling_settings, system_user
    ):
        user = JheUser.objects.create_user(email="bare@example.org", password="x", user_type="patient")
        user.patient.organizations.add(organization)
        ing, skip, errored = ingest_for_user(user.patient.id)
        assert (ing, skip, errored) == (0, 0, True)
        event = OWPollEvent.objects.get(patient=user.patient)
        assert event.status == "errored"
        assert "no ow:* identifier" in event.error_message

    def test_ingest_for_user_with_unknown_patient_id_raises(self, polling_settings, db):
        with pytest.raises(Patient.DoesNotExist):
            ingest_for_user(99999999)

    @patch("core.services.ow_ingest.orchestrator_normalized.ow_service")
    def test_no_data_source_fails_patient_at_top_level(self, mock_ow, organization, polling_settings, system_user):
        """No DataSource fails the patient at the top level."""
        _wire_canned_ow(mock_ow)
        user = JheUser.objects.create_user(
            email="no-device@example.org",
            password="x",
            user_type="patient",
            identifier="ow:no-device",
        )
        user.patient.organizations.add(organization)
        study = create_study(
            organization=organization,
            codes=[
                "omh:heart-rate:2.0",
                "omh:heart-rate-variability:1.0",
                "omh:oxygen-saturation:2.0",
                "omh:step-count:3.0",
                "omh:sleep-duration:2.0",
                "omh:sleep-episode:1.1",
                "omh:physical-activity:1.2",
            ],
        )
        add_patient_to_study(patient=user.patient, study=study, consent=True)
        # Note: NO StudyDataSource — that's what we're testing.

        ing, skip, errored = ingest_for_user(user.patient.id)
        assert errored is True
        assert (ing, skip) == (0, 0)
        # No OWPollStatus rows should have been created — we never even
        # entered the per-data-type loop.
        assert OWPollStatus.objects.filter(patient=user.patient).count() == 0
        event = OWPollEvent.objects.get(patient=user.patient)
        assert event.status == "errored"
        assert "personal_device" in event.error_message


# ---------------------------------------------------------------------------
# 3. Backfill semantics
# ---------------------------------------------------------------------------


class TestBackfillSemantics:
    @patch("core.services.ow_ingest.orchestrator_normalized.ow_service")
    def test_empty_response_keeps_backfill_incomplete(self, mock_ow, ow_patient, polling_settings, system_user):
        """Empty response keeps backfill_complete=False."""
        mock_ow.fetch_timeseries.side_effect = lambda *a, **kw: []
        mock_ow.fetch_summaries.side_effect = lambda *a, **kw: []
        mock_ow.fetch_sleep_details.side_effect = lambda *a, **kw: []

        call_command("ow_poll", f"--patient-id={ow_patient.id}")

        for s in OWPollStatus.objects.filter(patient=ow_patient):
            assert s.backfill_complete is False, f"{s.data_type} should remain incomplete after empty poll"
            assert s.last_success_at is not None  # we DID succeed

    @patch("core.services.ow_ingest.orchestrator_normalized.ow_service")
    def test_initial_backfill_window_is_30_days(self, mock_ow, ow_patient, polling_settings, system_user):
        """First poll uses 30-day backfill window."""
        captured = {}

        def capture_summaries(uid, start, end):
            captured["start"] = start
            captured["end"] = end
            return [
                {
                    **CANNED_OW["step_count"],
                    **CANNED_OW["sleep_duration"],
                    **CANNED_OW["physical_activity"],
                }
            ]

        mock_ow.fetch_summaries.side_effect = capture_summaries
        mock_ow.fetch_timeseries.side_effect = lambda *a, **kw: []
        mock_ow.fetch_sleep_details.side_effect = lambda *a, **kw: []

        call_command("ow_poll", f"--patient-id={ow_patient.id}")

        delta = captured["end"] - captured["start"]
        assert 29 <= delta.days <= 31, f"expected ~30d window, got {delta.days}d"

    @patch("core.services.ow_ingest.orchestrator_normalized.ow_service")
    def test_subsequent_poll_uses_lookback_window(self, mock_ow, ow_patient, polling_settings, system_user):
        """After backfill_complete, uses 7-day lookback."""
        _wire_canned_ow(mock_ow)
        call_command("ow_poll", f"--patient-id={ow_patient.id}")

        # Second run — capture the window
        captured = {}

        def capture_summaries(uid, start, end):
            captured["start"] = start
            captured["end"] = end
            return [
                {
                    **CANNED_OW["step_count"],
                    **CANNED_OW["sleep_duration"],
                    **CANNED_OW["physical_activity"],
                }
            ]

        mock_ow.fetch_summaries.side_effect = capture_summaries
        call_command("ow_poll", f"--patient-id={ow_patient.id}")

        delta = captured["end"] - captured["start"]
        assert 6 <= delta.days <= 8, f"expected ~7d window, got {delta.days}d"

    @patch("core.services.ow_ingest.orchestrator_normalized.ow_service")
    def test_force_backfill_uses_30d_even_when_complete(self, mock_ow, ow_patient, polling_settings, system_user):
        _wire_canned_ow(mock_ow)
        # First, mark a poll status as complete
        call_command("ow_poll", f"--patient-id={ow_patient.id}")
        assert OWPollStatus.objects.filter(patient=ow_patient, backfill_complete=True).count() == 7

        # Now force-backfill — must widen the window despite backfill_complete=True
        captured = {}

        def capture_summaries(uid, start, end):
            captured["start"] = start
            captured["end"] = end
            return [
                {
                    **CANNED_OW["step_count"],
                    **CANNED_OW["sleep_duration"],
                    **CANNED_OW["physical_activity"],
                }
            ]

        mock_ow.fetch_summaries.side_effect = capture_summaries
        call_command("ow_poll", f"--patient-id={ow_patient.id}", "--force-backfill")

        delta = captured["end"] - captured["start"]
        assert 29 <= delta.days <= 31

    @patch("core.services.ow_ingest.orchestrator_normalized.ow_service")
    def test_force_backfill_does_not_persist_to_disk(self, mock_ow, ow_patient, polling_settings, system_user):
        """force_backfill doesn't regress backfill_complete on disk."""
        _wire_canned_ow(mock_ow)
        call_command("ow_poll", f"--patient-id={ow_patient.id}")
        assert OWPollStatus.objects.filter(patient=ow_patient, backfill_complete=True).count() == 7

        call_command("ow_poll", f"--patient-id={ow_patient.id}", "--force-backfill")

        # Force-backfill ran but backfill_complete should still be True on
        # disk (the run itself was successful so it set it back to True too,
        # but the key invariant is: it must NEVER be False between the
        # mutation and the save).
        assert OWPollStatus.objects.filter(patient=ow_patient, backfill_complete=True).count() == 7


# ---------------------------------------------------------------------------
# 4. Cache fan-out and dedup batch query
# ---------------------------------------------------------------------------


class TestCacheAndDedup:
    @patch("core.services.ow_ingest.orchestrator_normalized.ow_service")
    def test_summaries_fetched_once_per_patient(self, mock_ow, ow_patient, polling_settings, system_user):
        """/summaries fetched once, reused for all summary data types."""
        _wire_canned_ow(mock_ow)
        call_command("ow_poll", f"--patient-id={ow_patient.id}")
        assert mock_ow.fetch_summaries.call_count == 1

    @patch("core.services.ow_ingest.orchestrator_normalized.ow_service")
    def test_timeseries_fetched_once_per_patient(self, mock_ow, ow_patient, polling_settings, system_user):
        """heart_rate + heart_rate_variability + oxygen_saturation share the /timeseries call."""
        _wire_canned_ow(mock_ow)
        call_command("ow_poll", f"--patient-id={ow_patient.id}")
        assert mock_ow.fetch_timeseries.call_count == 1
        # The call passed both types in a single request
        first_call_types = mock_ow.fetch_timeseries.call_args.args[1]
        assert set(first_call_types) == {"heart_rate", "heart_rate_variability", "oxygen_saturation"}

    @patch("core.services.ow_ingest.orchestrator_normalized.ow_service")
    def test_404_on_summaries_caches_empty_for_siblings(self, mock_ow, ow_patient, polling_settings, system_user):
        """404 on /summaries caches empty list for sibling types."""
        resp_404 = Mock()
        resp_404.status_code = 404
        mock_ow.fetch_summaries.side_effect = requests.HTTPError(response=resp_404)
        mock_ow.fetch_timeseries.side_effect = lambda *a, **kw: []
        mock_ow.fetch_sleep_details.side_effect = lambda *a, **kw: []

        call_command("ow_poll", f"--patient-id={ow_patient.id}")

        # /summaries should have been called exactly ONCE despite 3 data
        # types using it — the cached empty list short-circuits the rest.
        assert mock_ow.fetch_summaries.call_count == 1

    @patch("core.services.ow_ingest.orchestrator_normalized.fetch_existing_effective_dts")
    @patch("core.services.ow_ingest.orchestrator_normalized.ow_service")
    def test_dedup_uses_batch_fetch_one_call_per_data_type(
        self, mock_ow, mock_batch, ow_patient, polling_settings, system_user
    ):
        """Batch dedup: one query per data_type, not per record."""
        _wire_canned_ow(mock_ow)
        mock_batch.return_value = set()  # no existing dts
        call_command("ow_poll", f"--patient-id={ow_patient.id}")

        assert mock_batch.call_count == 7

    @patch("core.services.ow_ingest.orchestrator_normalized.ow_service")
    def test_dedup_real_query_skips_known_timestamp(self, mock_ow, ow_patient, polling_settings, system_user):
        """Same timestamp on second run → zero new records."""
        _wire_canned_ow(mock_ow)
        call_command("ow_poll", f"--patient-id={ow_patient.id}")
        first_count = Observation.objects.filter(subject_patient=ow_patient).count()
        assert first_count == 7

        # Second run with the same canned data
        call_command("ow_poll", f"--patient-id={ow_patient.id}")
        assert Observation.objects.filter(subject_patient=ow_patient).count() == 7

        # Now wire a NEW heart_rate sample with a DIFFERENT timestamp and
        # verify that one DOES land (proves the dedup query is timestamp-
        # specific, not "have we ingested any heart_rate records").
        new_sample = {
            "timestamp": "2026-04-09T09:30:00+00:00",  # different
            "type": "heart_rate",
            "value": 75,
            "unit": "bpm",
        }
        mock_ow.fetch_timeseries.side_effect = lambda u, types, s, e: [new_sample]
        mock_ow.fetch_summaries.side_effect = lambda u, s, e: []
        mock_ow.fetch_sleep_details.side_effect = lambda u, s, e: []
        call_command("ow_poll", f"--patient-id={ow_patient.id}")
        assert (
            Observation.objects.filter(
                subject_patient=ow_patient,
                codeable_concept__coding_code="omh:heart-rate:2.0",
            ).count()
            == 2
        )


# ---------------------------------------------------------------------------
# 5. Webhook view
# ---------------------------------------------------------------------------


class TestOWWebhook:
    def _post(self, body: dict, secret: str | None = None, content_length: int | None = None):
        client = APIClient()
        raw = json.dumps(body).encode("utf-8")
        headers = {}
        if secret:
            sig = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
            headers["HTTP_X_SIGNATURE"] = f"sha256={sig}"
        if content_length is not None:
            headers["CONTENT_LENGTH"] = str(content_length)
        return client.post(
            "/api/v1/ow/webhook",
            data=raw,
            content_type="application/json",
            **headers,
        )

    @patch("core.views.ow_webhook.ingest_for_user")
    def test_polling_mode_always_returns_401(self, mock_ingest, polling_settings):
        """In polling mode the secret is empty — auth always fails (401, not
        503), preventing side-channel detection of the active mode. Also
        covers the empty-secret-with-empty-signature edge case."""
        # Normal attempt with a real secret
        resp = self._post({"user_id": "anything"}, secret="test-secret")
        assert resp.status_code == 401

        # HMAC computed against the empty key (the polling-mode default)
        raw = json.dumps({"user_id": "x"}).encode("utf-8")
        sig = hmac.new(b"", raw, hashlib.sha256).hexdigest()
        client = APIClient()
        resp = client.post(
            "/api/v1/ow/webhook",
            data=raw,
            content_type="application/json",
            HTTP_X_SIGNATURE=f"sha256={sig}",
        )
        assert resp.status_code == 401
        mock_ingest.assert_not_called()

    @patch("core.views.ow_webhook.ingest_for_user")
    def test_returns_413_on_oversize_payload(self, mock_ingest, webhook_settings):
        """Declared Content-Length above the 64 KB cap must be rejected
        before HMAC verification."""
        resp = self._post({"user_id": "x"}, secret=webhook_settings, content_length=200000)
        assert resp.status_code == 413
        mock_ingest.assert_not_called()

    @patch("core.views.ow_webhook.ingest_for_user")
    def test_bad_auth_returns_401(self, mock_ingest, webhook_settings):
        """Missing signature and wrong signature both yield 401."""
        resp = self._post({"user_id": "anything"})
        assert resp.status_code == 401

        resp = self._post({"user_id": "anything"}, secret="wrong-secret")
        assert resp.status_code == 401
        mock_ingest.assert_not_called()

    @patch("core.views.ow_webhook.ingest_for_user")
    def test_returns_404_on_unknown_user_does_not_dispatch(self, mock_ingest, webhook_settings):
        resp = self._post({"user_id": "unknown-uuid"}, secret=webhook_settings)
        assert resp.status_code == 404
        mock_ingest.assert_not_called()

    @patch("core.views.ow_webhook.ingest_for_user")
    def test_malformed_payload(self, mock_ingest, webhook_settings):
        """Missing user_id field, empty body, and non-JSON body all fail."""
        # Missing user_id
        resp = self._post({"not_user_id": "x"}, secret=webhook_settings)
        assert resp.status_code == 400

        # Empty body (no valid signature possible)
        client = APIClient()
        resp = client.post("/api/v1/ow/webhook", data=b"", content_type="application/json")
        assert resp.status_code == 401

        # Valid HMAC over non-JSON body
        body = b"not json at all"
        sig = hmac.new(webhook_settings.encode("utf-8"), body, hashlib.sha256).hexdigest()
        resp = client.post(
            "/api/v1/ow/webhook",
            data=body,
            content_type="application/json",
            HTTP_X_SIGNATURE=f"sha256={sig}",
        )
        assert resp.status_code == 400
        mock_ingest.assert_not_called()

    @patch("core.views.ow_webhook.ingest_for_user")
    def test_dispatches_via_background_thread_returns_202(self, mock_ingest, webhook_settings, ow_patient, system_user):
        """The webhook returns 202 immediately and dispatches ingest in a
        background daemon thread that actually calls ingest_for_user."""
        mock_ingest.return_value = (5, 0, False)
        resp = self._post({"user_id": "test-uuid-12345"}, secret=webhook_settings)
        assert resp.status_code == 202

        # Give the daemon thread a brief moment to fire.
        deadline = time.time() + 2.0
        while time.time() < deadline and mock_ingest.call_count == 0:
            time.sleep(0.01)
        mock_ingest.assert_called_once_with(ow_patient.id, trigger="webhook")


# ---------------------------------------------------------------------------
# 6. Config validator
# ---------------------------------------------------------------------------


class TestPollingConfig:
    def _wipe(self):
        from django.core.cache import cache

        JheSetting.objects.filter(key__startswith="ow.").delete()
        cache.clear()

    def test_missing_api_base_url_raises(self, db):
        self._wipe()
        with pytest.raises(ImproperlyConfigured, match="api_base_url"):
            load_and_validate_polling_config()

    def test_invalid_url_scheme_raises(self, db):
        self._wipe()
        set_jhe_settings(**{"ow.api_base_url": "not-a-url", "ow.api_key": "k"})
        with pytest.raises(ImproperlyConfigured, match="api_base_url"):
            load_and_validate_polling_config()

    def test_missing_api_key_raises(self, db):
        self._wipe()
        set_jhe_settings(**{"ow.api_base_url": "http://ow.test"})
        with pytest.raises(ImproperlyConfigured, match="api_key"):
            load_and_validate_polling_config()

    def test_lookback_days_too_small_raises(self, db):
        self._wipe()
        set_jhe_settings(
            **{
                "ow.api_base_url": "http://ow.test",
                "ow.api_key": "k",
                "ow.lookback_days": "1",
            }
        )
        with pytest.raises(ImproperlyConfigured, match="lookback_days"):
            load_and_validate_polling_config()

    def test_initial_backfill_smaller_than_lookback_raises(self, db):
        self._wipe()
        set_jhe_settings(
            **{
                "ow.api_base_url": "http://ow.test",
                "ow.api_key": "k",
                "ow.lookback_days": "7",
                "ow.initial_backfill_days": "3",
            }
        )
        with pytest.raises(ImproperlyConfigured, match="initial_backfill_days"):
            load_and_validate_polling_config()

    def test_invalid_ingest_mode_raises(self, db):
        self._wipe()
        set_jhe_settings(
            **{
                "ow.api_base_url": "http://ow.test",
                "ow.api_key": "k",
                "ow.ingest_mode": "garbage",
            }
        )
        with pytest.raises(ImproperlyConfigured, match="ingest_mode"):
            load_and_validate_polling_config()

    def test_webhook_mode_without_secret_raises(self, db):
        self._wipe()
        set_jhe_settings(
            **{
                "ow.api_base_url": "http://ow.test",
                "ow.api_key": "k",
                "ow.ingest_mode": "webhook",
            }
        )
        with pytest.raises(ImproperlyConfigured, match="webhook_secret"):
            load_and_validate_polling_config()

    def test_valid_polling_config_returns_dict(self, polling_settings):
        cfg = load_and_validate_polling_config()
        assert cfg["api_base_url"] == "http://ow.test"
        assert cfg["api_key"] == "test-key"
        assert cfg["lookback_days"] == 7
        assert cfg["initial_backfill_days"] == 30
        assert cfg["ingest_mode"] == "polling"


# ---------------------------------------------------------------------------
# 7. Truncation of error strings
# ---------------------------------------------------------------------------


class TestErrorTruncation:
    @patch("core.services.ow_ingest.orchestrator_normalized.ow_service")
    def test_long_http_error_is_truncated_in_last_error(self, mock_ow, ow_patient, polling_settings, system_user):
        """A megabyte-sized error body must be truncated before being
        written to OWPollStatus.last_error."""
        big = "x" * 5000
        mock_ow.fetch_timeseries.side_effect = requests.ConnectionError(big)
        mock_ow.fetch_summaries.side_effect = requests.ConnectionError(big)
        mock_ow.fetch_sleep_details.side_effect = requests.ConnectionError(big)

        call_command("ow_poll", f"--patient-id={ow_patient.id}")

        for s in OWPollStatus.objects.filter(patient=ow_patient):
            assert len(s.last_error) <= orch.ERROR_STRING_MAX_LEN
            assert s.last_error.endswith("...truncated")

    @patch("core.services.ow_ingest.orchestrator_normalized.ow_service")
    def test_owpollevent_error_message_is_truncated(self, mock_ow, ow_patient, polling_settings, system_user):
        """OWPollEvent.error_message joins per-data-type errors and must
        also be capped at ERROR_MESSAGE_MAX_LEN."""
        big = "x" * 5000
        mock_ow.fetch_timeseries.side_effect = requests.ConnectionError(big)
        mock_ow.fetch_summaries.side_effect = requests.ConnectionError(big)
        mock_ow.fetch_sleep_details.side_effect = requests.ConnectionError(big)

        call_command("ow_poll", f"--patient-id={ow_patient.id}")

        event = OWPollEvent.objects.get(patient=ow_patient)
        assert len(event.error_message) <= orch.ERROR_MESSAGE_MAX_LEN
        assert event.error_message.endswith("...truncated")


# ---------------------------------------------------------------------------
# 8. OWPollStatus.disabled escape hatch
# ---------------------------------------------------------------------------


class TestDisabledFlag:
    @patch("core.services.ow_ingest.orchestrator_normalized.ow_service")
    def test_disabled_data_type_is_skipped_without_touching_state(
        self, mock_ow, ow_patient, polling_settings, system_user
    ):
        _wire_canned_ow(mock_ow)
        OWPollStatus.objects.create(patient=ow_patient, data_type="heart_rate", disabled=True)

        call_command("ow_poll", f"--patient-id={ow_patient.id}")

        # heart_rate must NOT have an Observation
        assert not Observation.objects.filter(
            subject_patient=ow_patient,
            codeable_concept__coding_code="omh:heart-rate:2.0",
        ).exists()
        # The other 6 data types should still have ingested
        assert Observation.objects.filter(subject_patient=ow_patient).count() == 6
        # The disabled row's last_poll_at should remain None (we never
        # touched it).
        hr_status = OWPollStatus.objects.get(patient=ow_patient, data_type="heart_rate")
        assert hr_status.last_poll_at is None
        assert hr_status.disabled is True

    @patch("core.services.ow_ingest.orchestrator_normalized.ow_service")
    def test_disabled_after_previous_success_preserves_history(
        self, mock_ow, ow_patient, polling_settings, system_user
    ):
        """Disabled tuple preserves last_poll_at and last_success_at."""
        _wire_canned_ow(mock_ow)
        # First successful run
        call_command("ow_poll", f"--patient-id={ow_patient.id}")
        hr = OWPollStatus.objects.get(patient=ow_patient, data_type="heart_rate")
        original_last_poll = hr.last_poll_at
        original_last_success = hr.last_success_at
        assert original_last_poll is not None
        assert original_last_success is not None

        # Disable it
        hr.disabled = True
        hr.save()

        # Re-poll
        call_command("ow_poll", f"--patient-id={ow_patient.id}")

        hr.refresh_from_db()
        # last_poll_at and last_success_at must NOT have moved.
        assert hr.last_poll_at == original_last_poll
        assert hr.last_success_at == original_last_success
        assert hr.disabled is True
        # Still 7 observations (the disabled HR didn't add a new one,
        # the other 6 are dedup hits).
        assert Observation.objects.filter(subject_patient=ow_patient).count() == 7


# ---------------------------------------------------------------------------
# 9. Consent-scope filtering
# ---------------------------------------------------------------------------


class TestConsentScopeFiltering:
    @patch("core.services.ow_ingest.orchestrator_normalized.ow_service")
    def test_only_consented_data_types_are_ingested(self, mock_ow, organization, polling_settings, system_user):
        """A patient who consented to only heart_rate and oxygen_saturation
        should only get those two data types ingested — even though OW has
        data for all types."""
        user = JheUser.objects.create_user(
            email="partial-consent@example.org",
            password="x",
            user_type="patient",
            identifier="ow:partial-uuid",
        )
        user.patient.organizations.add(organization)
        study = create_study(
            organization=organization,
            codes=[
                "omh:heart-rate:2.0",
                "omh:heart-rate-variability:1.0",
                "omh:oxygen-saturation:2.0",
                "omh:step-count:3.0",
                "omh:sleep-duration:2.0",
                "omh:sleep-episode:1.1",
                "omh:physical-activity:1.2",
            ],
        )
        # Enroll WITHOUT auto-consent, then consent to only 2 scopes.
        add_patient_to_study(patient=user.patient, study=study, consent=False)
        from django.utils import timezone as tz

        from core.models import StudyPatient, StudyPatientScopeConsent, StudyScopeRequest

        sp = StudyPatient.objects.get(study=study, patient=user.patient)
        for sr in StudyScopeRequest.objects.filter(study=study):
            code = sr.scope_code.coding_code
            StudyPatientScopeConsent.objects.create(
                study_patient=sp,
                scope_code=sr.scope_code,
                consented=code in ("omh:heart-rate:2.0", "omh:oxygen-saturation:2.0"),
                consented_time=tz.now(),
            )
        device = DataSource.objects.create(name="Oura (partial)", type="personal_device")
        StudyDataSource.objects.create(study=study, data_source=device)

        _wire_canned_ow(mock_ow)
        call_command("ow_poll", f"--patient-id={user.patient.id}")

        actual_codes = set(
            Observation.objects.filter(subject_patient=user.patient).values_list(
                "codeable_concept__coding_code", flat=True
            )
        )
        assert actual_codes == {"omh:heart-rate:2.0", "omh:oxygen-saturation:2.0"}
        assert Observation.objects.filter(subject_patient=user.patient).count() == 2

        event = OWPollEvent.objects.get(patient=user.patient)
        assert event.status == "completed"
        assert event.records_ingested == 2
        assert event.records_skipped == 0

    @patch("core.services.ow_ingest.orchestrator_normalized.ow_service")
    def test_no_consented_scopes_fails_event(self, mock_ow, organization, polling_settings, system_user):
        """A patient with zero consented scopes should error without calling OW."""
        user = JheUser.objects.create_user(
            email="no-consent-scopes@example.org",
            password="x",
            user_type="patient",
            identifier="ow:no-scopes-uuid",
        )
        user.patient.organizations.add(organization)
        study = create_study(
            organization=organization,
            codes=["omh:heart-rate:2.0"],
        )
        # Enroll but don't consent to anything.
        add_patient_to_study(patient=user.patient, study=study, consent=False)
        device = DataSource.objects.create(name="Oura (none)", type="personal_device")
        StudyDataSource.objects.create(study=study, data_source=device)

        _wire_canned_ow(mock_ow)
        call_command("ow_poll", f"--patient-id={user.patient.id}")

        assert Observation.objects.filter(subject_patient=user.patient).count() == 0
        mock_ow.fetch_timeseries.assert_not_called()
        mock_ow.fetch_summaries.assert_not_called()

        event = OWPollEvent.objects.get(patient=user.patient)
        assert event.status == "errored"
        assert "no consented data types" in event.error_message
