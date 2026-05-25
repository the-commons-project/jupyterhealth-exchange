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


def test_observation_from_fhir_entry():
    entry = {
        "resource": {
            "resourceType": "Observation",
            "id": "obs-1",
            "code": {"coding": [{"system": "http://loinc.org", "code": "2339-0", "display": "Glucose"}]},
            "effectiveDateTime": "2026-04-15T08:00:00Z",
            "valueQuantity": {"value": 92, "unit": "mg/dL"},
            "subject": {"reference": "Patient/7"},
        }
    }
    o = Observation.from_fhir_entry(entry)
    assert o.observation_id == "obs-1"
    assert o.code == "2339-0"
    assert o.code_system == "http://loinc.org"
    assert o.effective_at == "2026-04-15T08:00:00Z"
    assert o.value == 92
    assert o.unit == "mg/dL"
    assert o.patient_id == "7"
