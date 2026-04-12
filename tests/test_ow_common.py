"""Tests for core.services.ow_ingest._common shared helpers."""

import base64
import json
import uuid

import pytest

from core.models import CodeableConcept, DataSource, Observation, StudyDataSource
from core.services.ow_ingest._common import (
    _extract_effective_dt,
    _resolve_data_source,
    _truncate,
    build_polling_set,
    consented_data_types,
    fetch_existing_effective_dts,
    resolve_ow_user_id,
    resolve_patient_from_ow_user_id,
    wrap_omh_as_fhir,
)

from .utils import Code, add_patient_to_study, create_study


class TestTruncate:
    def test_short_string_unchanged(self):
        assert _truncate("hello", 100) == "hello"

    def test_exact_limit_unchanged(self):
        assert _truncate("a" * 50, 50) == "a" * 50

    def test_long_string_truncated(self):
        result = _truncate("x" * 100, 30)
        assert len(result) == 30
        assert result.endswith(" ...truncated")

    def test_limit_smaller_than_marker(self):
        assert _truncate("x" * 100, 5) == "xxxxx"


class TestResolveOwUserId:
    @pytest.mark.parametrize(
        "identifier, expected",
        [
            ("ow:abc-123", "abc-123"),
            ("some-other-id", None),
            ("", None),
        ],
    )
    def test_resolve(self, patient, identifier, expected):
        patient.jhe_user.identifier = identifier
        patient.jhe_user.save()
        assert resolve_ow_user_id(patient) == expected


class TestResolvePatientFromOwUserId:
    def test_found(self, patient):
        ow_id = str(uuid.uuid4())
        patient.jhe_user.identifier = f"ow:{ow_id}"
        patient.jhe_user.save()
        result = resolve_patient_from_ow_user_id(ow_id)
        assert result is not None
        assert result.pk == patient.pk

    def test_not_found(self, db):
        assert resolve_patient_from_ow_user_id("nonexistent-id") is None


class TestConsentedDataTypes:
    def test_no_consents_returns_empty(self, patient):
        mapping = {"heart_rate": "omh:heart-rate:2.0", "steps": "omh:step-count:3.0"}
        assert consented_data_types(patient, mapping) == frozenset()

    def test_with_consent(self, patient, organization):
        study = create_study(organization=organization, codes=[Code.HeartRate])
        add_patient_to_study(patient=patient, study=study)
        mapping = {"heart_rate": "omh:heart-rate:2.0", "steps": "omh:step-count:3.0"}
        assert consented_data_types(patient, mapping) == frozenset({"heart_rate"})


class TestBuildPollingSet:
    def test_included_with_ow_id_and_consent(self, patient, organization):
        patient.jhe_user.identifier = "ow:some-uuid"
        patient.jhe_user.save()
        study = create_study(organization=organization, codes=[Code.HeartRate])
        add_patient_to_study(patient=patient, study=study)
        assert patient in build_polling_set()

    def test_excluded_without_ow_id(self, patient, organization):
        patient.jhe_user.identifier = "not-ow"
        patient.jhe_user.save()
        study = create_study(organization=organization, codes=[Code.HeartRate])
        add_patient_to_study(patient=patient, study=study)
        assert patient not in build_polling_set()

    def test_excluded_without_consent(self, patient):
        patient.jhe_user.identifier = "ow:some-uuid"
        patient.jhe_user.save()
        assert patient not in build_polling_set()


class TestExtractEffectiveDt:
    @pytest.mark.parametrize(
        "record, expected",
        [
            ({"effective_time_frame": {"date_time": "2024-01-15T10:30:00Z"}}, "2024-01-15T10:30:00Z"),
            (
                {"effective_time_frame": {"time_interval": {"start_date_time": "2024-01-15T08:00:00Z"}}},
                "2024-01-15T08:00:00Z",
            ),
            ({}, None),
            ({"effective_time_frame": {}}, None),
        ],
    )
    def test_extract(self, record, expected):
        assert _extract_effective_dt(record) == expected


class TestWrapOmhAsFhir:
    def test_produces_valid_fhir_dict(self, patient, device):
        cc = CodeableConcept.objects.create(
            coding_system="https://w3id.org/openmhealth",
            coding_code="omh:heart-rate:2.0",
            text="Heart Rate",
        )
        omh_record = {
            "body": {
                "heart_rate": {"value": 72, "unit": "beats/min"},
                "effective_time_frame": {"date_time": "2024-01-15T10:30:00Z"},
            }
        }
        result = wrap_omh_as_fhir(omh_record, patient, device, cc)

        assert result["resourceType"] == "Observation"
        assert result["status"] == "final"
        assert result["subject"]["reference"] == f"Patient/{patient.id}"
        assert result["device"]["reference"] == f"Device/{device.id}"
        assert result["effectiveDateTime"] == "2024-01-15T10:30:00Z"
        decoded = json.loads(base64.b64decode(result["valueAttachment"]["data"]))
        assert decoded == omh_record

    def test_no_body_key_uses_record_as_body(self, patient, device):
        cc = CodeableConcept.objects.create(
            coding_system="https://w3id.org/openmhealth",
            coding_code="omh:test:1.0",
            text="Test",
        )
        omh_record = {
            "heart_rate": {"value": 72},
            "effective_time_frame": {"date_time": "2024-01-15T10:30:00Z"},
        }
        result = wrap_omh_as_fhir(omh_record, patient, device, cc)
        assert result["effectiveDateTime"] == "2024-01-15T10:30:00Z"


class TestFetchExistingEffectiveDts:
    def test_returns_stored_timestamps(self, patient, device):
        cc = CodeableConcept.objects.create(
            coding_system="https://w3id.org/openmhealth",
            coding_code="omh:hr-dedup:2.0",
            text="HR Dedup",
        )
        Observation.objects.bulk_create(
            [
                Observation(
                    subject_patient=patient,
                    codeable_concept=cc,
                    data_source=device,
                    value_attachment_data={"body": {"effective_time_frame": {"date_time": "2024-01-15T10:30:00Z"}}},
                )
            ]
        )
        assert "2024-01-15T10:30:00Z" in fetch_existing_effective_dts(patient, cc, device)

    def test_empty_when_no_observations(self, patient, device):
        cc = CodeableConcept.objects.create(
            coding_system="https://w3id.org/openmhealth",
            coding_code="omh:empty:1.0",
            text="Empty",
        )
        assert fetch_existing_effective_dts(patient, cc, device) == set()


class TestResolveDataSource:
    def test_finds_data_source_on_patient_study(self, patient, organization):
        study = create_study(organization=organization, codes=[Code.HeartRate])
        add_patient_to_study(patient=patient, study=study)
        ds = DataSource.objects.create(name="OW Device", type="personal_device")
        StudyDataSource.objects.create(study=study, data_source=ds)
        result = _resolve_data_source(patient)
        assert result is not None
        assert result.pk == ds.pk

    def test_returns_none_when_no_data_source(self, patient):
        assert _resolve_data_source(patient) is None
