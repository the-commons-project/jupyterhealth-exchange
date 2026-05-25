from jhe_mcp.fhir.models import Demographics, Observation, StudyMeta


def test_study_meta_from_admin_payload():
    payload = {
        "id": 1,
        "name": "Test Study",
        "description": "demo",
        "organization": {"id": 42, "name": "Acme"},
    }
    sm = StudyMeta.from_admin(payload)
    assert sm.study_id == "1"
    assert sm.name == "Test Study"
    assert sm.organization_id == "42"
    assert sm.organization_name == "Acme"


def test_demographics_from_admin_patient():
    # JHE Admin API returns Patient in camelCase via djangorestframework-camel-case.
    # Field names verified during 2026-05-19 spike.
    payload = {
        "id": 7,
        "nameGiven": "Jane",
        "nameFamily": "Doe",
        "birthDate": "1990-04-12",
        "telecomEmail": "jane@example.com",
    }
    d = Demographics.from_admin(payload)
    assert d.patient_id == "7"
    assert d.given_name == "Jane"
    assert d.family_name == "Doe"
    assert d.birth_date == "1990-04-12"


def test_observation_from_fhir_entry_with_omh_body():
    entry = {
        "resource": {
            "resourceType": "Observation",
            "id": "obs-1",
            "code": {
                "coding": [
                    {"system": "https://w3id.org/openmhealth", "code": "omh:blood-pressure:4.0"}
                ]
            },
            "subject": {"reference": "Patient/7"},
            "valueAttachment": {
                "body": {
                    "systolic_blood_pressure": {"unit": "mmHg", "value": 120},
                    "diastolic_blood_pressure": {"unit": "mmHg", "value": 80},
                    "effective_time_frame": {"date_time": "2026-04-15T08:00:00Z"},
                },
                "header": {"schema_id": {"name": "blood-pressure", "version": "4.0"}},
            },
        }
    }
    o = Observation.from_fhir_entry(entry)
    assert o.observation_id == "obs-1"
    assert o.code == "omh:blood-pressure:4.0"
    assert o.code_system == "https://w3id.org/openmhealth"
    assert o.effective_at == "2026-04-15T08:00:00Z"
    assert o.patient_id == "7"
    assert o.omh_body["systolic_blood_pressure"]["value"] == 120
    assert o.omh_body["diastolic_blood_pressure"]["value"] == 80


def test_observation_from_fhir_entry_without_attachment():
    entry = {
        "resource": {
            "resourceType": "Observation",
            "id": "obs-2",
            "code": {"coding": [{"system": "http://loinc.org", "code": "2339-0"}]},
            "subject": {"reference": "Patient/7"},
        }
    }
    o = Observation.from_fhir_entry(entry)
    assert o.observation_id == "obs-2"
    assert o.omh_body is None
    assert o.effective_at is None
