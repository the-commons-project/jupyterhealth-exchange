"""Tests for R4 ingestion: the cross-version transform engine (core/fhir/xver) and the
/fhir-import/R4 endpoint (core/views/fhir_import.py).

The engine converts an R4 body to R5 by interpreting the bundled HL7 ``*4to5`` FML StructureMaps;
the endpoint then reuses the normal create routing (mapped-vs-aux, R5 validation, source header,
provenance). See fhir-r4-import.md (repo root).
"""

import uuid

import pytest

from core.fhir.cross_version import XVerError, dropped_field_paths, transform_to_r5
from core.fhir.fhir_validation import validate_fhir_resource
from core.models import FhirAuxResource, FhirSource


@pytest.fixture
def fhir_source(patient, device):
    return FhirSource.objects.create(
        patient=patient, data_source=device, label="Patient EHR", fhir_base_url="https://ehr.example/fhir"
    )


def _src(fhir_source):
    return {"HTTP_X_JHE_FHIR_SOURCE_ID": str(fhir_source.id)}


# ---------------------------------------------------------------------------
# Engine (no DB): shape correctness against real HL7 maps
# ---------------------------------------------------------------------------


def test_engine_observation_choice_and_backbone():
    r4 = {
        "resourceType": "Observation",
        "status": "final",
        "code": {"coding": [{"system": "http://loinc.org", "code": "85354-9"}]},
        "subject": {"reference": "Patient/p1"},
        "effectiveDateTime": "2023-01-15T10:30:00Z",  # choice element value[x]/effective[x]
        "valueQuantity": {"value": 72, "unit": "mmHg", "system": "http://unitsofmeasure.org", "code": "mm[Hg]"},
        "component": [  # repeating backbone element
            {
                "code": {"coding": [{"system": "http://loinc.org", "code": "8480-6"}]},
                "valueQuantity": {"value": 120, "system": "http://unitsofmeasure.org", "code": "mm[Hg]"},
            }
        ],
    }
    out = transform_to_r5("Observation", r4)
    validate_fhir_resource("Observation", out)  # raises on invalid R5

    assert out["resourceType"] == "Observation"
    # choice element preserved under its flattened key
    assert out["effectiveDateTime"] == "2023-01-15T10:30:00Z"
    assert out["valueQuantity"]["value"] == 72
    # backbone element (and its own nested choice) preserved
    assert out["component"][0]["code"]["coding"][0]["code"] == "8480-6"
    assert out["component"][0]["valueQuantity"]["value"] == 120


def test_engine_patient_repeating_primitives():
    r4 = {
        "resourceType": "Patient",
        "active": True,
        "name": [{"use": "official", "family": "Doe", "given": ["Jane", "Q"]}],
        "gender": "female",
        "birthDate": "1990-05-05",
        "address": [{"line": ["1 Main St", "Apt 2"], "city": "Boston"}],
    }
    out = transform_to_r5("Patient", r4)
    validate_fhir_resource("Patient", out)

    # repeating *primitive* elements must stay lists, not collapse to a scalar
    assert out["name"][0]["given"] == ["Jane", "Q"]
    assert out["name"][0]["family"] == "Doe"
    assert out["address"][0]["line"] == ["1 Main St", "Apt 2"]


def test_engine_condition_valid_r5():
    r4 = {
        "resourceType": "Condition",
        "clinicalStatus": {
            "coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-clinical", "code": "active"}]
        },
        "code": {"coding": [{"system": "http://snomed.info/sct", "code": "44054006"}], "text": "Diabetes"},
        "subject": {"reference": "Patient/p1"},
        "onsetDateTime": "2020-01-01",
    }
    out = transform_to_r5("Condition", r4)
    validate_fhir_resource("Condition", out)
    assert out["code"]["text"] == "Diabetes"
    assert out["onsetDateTime"] == "2020-01-01"


def test_engine_unknown_resource_raises():
    with pytest.raises(XVerError):
        transform_to_r5("NotAResource", {"resourceType": "NotAResource"})


# ---------------------------------------------------------------------------
# Dropped-field detection (leaf-value diff)
# ---------------------------------------------------------------------------


def test_dropped_field_paths_flags_true_loss_not_renames():
    r4 = {"resourceType": "X", "id": "1", "kept": "a", "moved": "b", "gone": "c"}
    r5 = {"resourceType": "X", "id": "1", "kept": "a", "renamed": "b"}  # 'moved' value survives as 'renamed'
    dropped = dropped_field_paths(r4, r5)
    assert dropped == ["gone"]  # only genuine loss; the rename (value 'b' preserved) is not flagged


def test_dropped_field_paths_collapses_dropped_subtree():
    r4 = {"resourceType": "X", "activity": [{"detail": {"status": "in-progress", "code": {"text": "walk"}}}]}
    r5 = {"resourceType": "X"}
    # the whole subtree is gone -> reported once at its root, not per leaf
    assert dropped_field_paths(r4, r5) == ["activity"]


# ---------------------------------------------------------------------------
# Endpoint: /fhir-import/R4 (single + Bundle), reusing the normal create path
# ---------------------------------------------------------------------------


def test_import_single_returns_bundle_and_stores_aux(api_client, patient, fhir_source):
    r4 = {
        "resourceType": "Condition",
        "id": "cond-r4",
        "clinicalStatus": {
            "coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-clinical", "code": "active"}]
        },
        "code": {"text": "Hypertension"},
        "subject": {"reference": f"Patient/{patient.id}"},
    }
    r = api_client.post("/fhir-import/R4/Condition", r4, **_src(fhir_source))
    # Import always returns a batch-response Bundle, even for a single resource.
    assert r.status_code == 200, r.text
    bundle = r.json()
    assert bundle["resourceType"] == "Bundle" and bundle["type"] == "batch-response"
    entry = bundle["entry"][0]
    assert entry["response"]["status"] == "201 Created"
    # A clean conversion carries an informational "no loss" outcome.
    assert entry["response"]["outcome"]["issue"][0]["severity"] == "information"
    created = entry["resource"]
    assert created["resourceType"] == "Condition"
    assert created["code"] == {"text": "Hypertension"}

    # Stored as an aux row (UUID id) linked to the source and its patient, with provenance.
    assert uuid.UUID(created["id"])
    aux = FhirAuxResource.objects.get(pk=created["id"])
    assert aux.resource_type == "Condition"
    assert aux.fhir_source_id == fhir_source.id
    assert aux.fhir_resource_id == "cond-r4"  # upstream id preserved
    assert aux.patient_fhir_id == str(patient.id)


def test_import_reports_dropped_fields_as_warnings(api_client, patient, fhir_source):
    # R4 CarePlan.activity.detail was removed in R5, so it is dropped -- but the CarePlan is still
    # valid R5 and created, with a warning naming the dropped path.
    r4 = {
        "resourceType": "CarePlan",
        "status": "active",
        "intent": "plan",
        "subject": {"reference": f"Patient/{patient.id}"},
        "activity": [{"detail": {"status": "in-progress", "description": "Walk 30 min daily"}}],
    }
    r = api_client.post("/fhir-import/R4/CarePlan", r4, **_src(fhir_source))
    assert r.status_code == 200, r.text
    entry = r.json()["entry"][0]
    assert entry["response"]["status"] == "201 Created"  # still created
    issues = entry["response"]["outcome"]["issue"]
    assert all(i["severity"] == "warning" for i in issues)
    assert any(i["expression"] == ["CarePlan.activity"] for i in issues)
    # The lost data really is absent from the stored resource.
    assert "activity" not in entry["resource"]


def test_import_requires_source_header(api_client, patient):
    r4 = {
        "resourceType": "Condition",
        "clinicalStatus": {
            "coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-clinical", "code": "active"}]
        },
        "subject": {"reference": f"Patient/{patient.id}"},
    }
    # Missing X-JHE-FHIR-Source-ID gates the whole request -> a real 400, not a per-entry outcome.
    assert api_client.post("/fhir-import/R4/Condition", r4).status_code == 400


def test_import_unsupported_resource_reports_error_entry(api_client, fhir_source):
    r = api_client.post("/fhir-import/R4/NotAResource", {"resourceType": "NotAResource"}, **_src(fhir_source))
    assert r.status_code == 200, r.text
    entry = r.json()["entry"][0]
    assert entry["response"]["status"].startswith("404")
    assert entry["response"]["outcome"]["issue"][0]["severity"] == "error"
    assert "resource" not in entry


def test_import_get_not_allowed(api_client, fhir_source):
    assert api_client.get("/fhir-import/R4/Condition", **_src(fhir_source)).status_code == 405


def test_import_bundle_batch_response(api_client, patient, fhir_source):
    bundle = {
        "resourceType": "Bundle",
        "type": "transaction",
        "entry": [
            {
                "resource": {
                    "resourceType": "Condition",
                    "clinicalStatus": {
                        "coding": [
                            {"system": "http://terminology.hl7.org/CodeSystem/condition-clinical", "code": "active"}
                        ]
                    },
                    "code": {"text": "Asthma"},
                    "subject": {"reference": f"Patient/{patient.id}"},
                }
            },
            {
                "resource": {
                    "resourceType": "Observation",
                    "status": "final",
                    "code": {"coding": [{"system": "http://loinc.org", "code": "8867-4"}]},
                    "subject": {"reference": f"Patient/{patient.id}"},
                    "valueQuantity": {"value": 80, "unit": "beats/min"},
                }
            },
        ],
    }
    r = api_client.post("/fhir-import/R4", bundle, **_src(fhir_source))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["type"] == "batch-response"
    statuses = [e["response"]["status"] for e in body["entry"]]
    assert statuses == ["201 Created", "201 Created"]
    # Every entry carries an OperationOutcome at response.outcome.
    assert all(e["response"]["outcome"]["resourceType"] == "OperationOutcome" for e in body["entry"])
    # Both landed in aux (non-OMH Observation falls through to aux, like the normal endpoint).
    assert FhirAuxResource.objects.filter(fhir_source=fhir_source).count() == 2


def test_import_bundle_reports_per_entry_error(api_client, patient, fhir_source):
    bundle = {
        "resourceType": "Bundle",
        "type": "batch",
        "entry": [
            {"resource": {"resourceType": "NotAResource"}},  # unsupported -> error entry
            {
                "resource": {
                    "resourceType": "Condition",
                    "clinicalStatus": {
                        "coding": [
                            {"system": "http://terminology.hl7.org/CodeSystem/condition-clinical", "code": "active"}
                        ]
                    },
                    "subject": {"reference": f"Patient/{patient.id}"},
                }
            },
        ],
    }
    r = api_client.post("/fhir-import/R4", bundle, **_src(fhir_source))
    assert r.status_code == 200, r.text
    entries = r.json()["entry"]
    assert entries[0]["response"]["outcome"]["resourceType"] == "OperationOutcome"
    assert entries[1]["response"]["status"] == "201 Created"
