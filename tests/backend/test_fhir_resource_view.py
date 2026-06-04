"""Tests for the unified FHIR resource endpoint (core/views/fhir.py): auxiliary-resource
CRUD, mapped-resource read-by-id, and the aux_fhir_data split on mapped writes."""

import base64
import json

import pytest
from django.core.exceptions import PermissionDenied
from rest_framework.test import APIClient

from core.models import FhirAuxResource, JheUser, Observation, Patient
from core.serializers import FHIRPatientSerializer
from core.utils import generate_observation_value_attachment_data

from .utils import Code, add_observations


def _condition(patient_id, **extra):
    return {"resourceType": "Condition", "subject": {"reference": f"Patient/{patient_id}"}, **extra}


# ---------------------------------------------------------------------------
# Auxiliary resource CRUD
# ---------------------------------------------------------------------------


def test_aux_create_and_read(api_client, patient):
    body = _condition(patient.id, code={"text": "Hypertension"}, clinicalStatus={"text": "active"})
    r = api_client.post("/FHIR/R5/Condition", body)
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["resourceType"] == "Condition"
    assert created["code"] == {"text": "Hypertension"}

    # The FHIR id is the Django pk; the row links the patient and keeps the original body.
    aux = FhirAuxResource.objects.get(pk=created["id"])
    assert aux.resource_type == "Condition"
    assert aux.patient_id == patient.id
    assert aux.patient_fhir_id == str(patient.id)
    assert aux.fhir_data["code"] == {"text": "Hypertension"}

    r = api_client.get(f"/FHIR/R5/Condition/{created['id']}")
    assert r.status_code == 200, r.text
    assert r.json() == created


def test_aux_search_returns_searchset_bundle(api_client, patient):
    for i in range(3):
        assert api_client.post("/FHIR/R5/Condition", _condition(patient.id, code={"text": f"c{i}"})).status_code == 201

    r = api_client.get("/FHIR/R5/Condition")
    assert r.status_code == 200, r.text
    bundle = r.json()
    assert bundle["resourceType"] == "Bundle"
    assert bundle["type"] == "searchset"
    assert bundle["total"] == 3
    assert {e["resource"]["resourceType"] for e in bundle["entry"]} == {"Condition"}


def test_aux_search_is_scoped_by_resource_type(api_client, patient):
    api_client.post("/FHIR/R5/Condition", _condition(patient.id))
    r = api_client.get("/FHIR/R5/QuestionnaireResponse")
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 0


def test_aux_put_replaces_and_patch_merges(api_client, patient):
    created = api_client.post(
        "/FHIR/R5/Condition", _condition(patient.id, code={"text": "old"}, recordedDate="2020-01-01")
    ).json()
    cid = created["id"]

    # PUT replaces the whole body.
    r = api_client.put(f"/FHIR/R5/Condition/{cid}", _condition(patient.id, code={"text": "new"}))
    assert r.status_code == 200, r.text
    assert r.json()["code"] == {"text": "new"}
    assert "recordedDate" not in r.json()

    # PATCH merges at the top level.
    r = api_client.patch(f"/FHIR/R5/Condition/{cid}", {"recordedDate": "2021-02-02"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["recordedDate"] == "2021-02-02"
    assert body["code"] == {"text": "new"}  # preserved from before


def test_aux_delete(api_client, patient):
    cid = api_client.post("/FHIR/R5/Condition", _condition(patient.id)).json()["id"]
    r = api_client.delete(f"/FHIR/R5/Condition/{cid}")
    assert r.status_code == 204, r.text
    assert not FhirAuxResource.objects.filter(pk=cid).exists()
    assert api_client.get(f"/FHIR/R5/Condition/{cid}").status_code == 404


def test_aux_unsupported_resource_type_404(api_client):
    assert api_client.get("/FHIR/R5/Bogus").status_code == 404
    assert api_client.post("/FHIR/R5/Bogus", {"resourceType": "Bogus"}).status_code == 404


def test_aux_create_unauthorized_patient_403(api_client, organization):
    # A patient the practitioner cannot access (different, unshared org).
    from core.models import Organization

    other = Organization.objects.create(name="Other", type="other")
    stranger = JheUser.objects.create_user(email="stranger@example.org", user_type="patient").patient
    stranger.organizations.add(other)

    r = api_client.post("/FHIR/R5/Condition", _condition(stranger.id))
    assert r.status_code == 403, r.text


def test_aux_patient_user_scoped_to_self(patient):
    client = APIClient()
    client.default_format = "json"
    client.force_authenticate(patient.jhe_user)

    # No subject reference: a patient user owns what they create.
    r = client.post("/FHIR/R5/Condition", {"resourceType": "Condition", "code": {"text": "self"}})
    assert r.status_code == 201, r.text
    assert FhirAuxResource.objects.get(pk=r.json()["id"]).patient_id == patient.id

    r = client.get("/FHIR/R5/Condition")
    assert r.status_code == 200
    assert r.json()["total"] == 1


# ---------------------------------------------------------------------------
# Mapped resource read-by-id
# ---------------------------------------------------------------------------


def test_observation_read_by_id(api_client, patient, hr_study):
    add_observations(patient=patient, code=Code.HeartRate, n=1)
    obs = Observation.objects.get(subject_patient=patient)
    r = api_client.get(f"/FHIR/R5/Observation/{obs.id}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["resourceType"] == "Observation"
    assert body["id"] == str(obs.id)
    assert body["subject"] == {"reference": f"Patient/{patient.id}"}


def test_observation_read_by_id_not_found(api_client, patient, hr_study):
    assert api_client.get("/FHIR/R5/Observation/999999").status_code == 404


def test_patient_read_by_id(api_client, patient):
    r = api_client.get(f"/FHIR/R5/Patient/{patient.id}")
    assert r.status_code == 200, r.text
    assert r.json()["resourceType"] == "Patient"
    assert r.json()["id"] == str(patient.id)


# ---------------------------------------------------------------------------
# aux_fhir_data split on mapped writes
# ---------------------------------------------------------------------------


def test_observation_create_preserves_unmapped_fields_in_aux(api_client, device, hr_study, patient):
    record = generate_observation_value_attachment_data(Code.HeartRate.value)
    resource = {
        "resourceType": "Observation",
        "status": "final",
        "code": {"coding": [{"system": Code.OpenMHealth.value, "code": Code.HeartRate.value}]},
        "subject": {"reference": f"Patient/{patient.id}"},
        "device": {"reference": f"Device/{device.id}"},
        "valueAttachment": {
            "contentType": "application/json",
            "data": base64.b64encode(json.dumps(record).encode()).decode(),
        },
        # Not in the config mapping -> must be preserved verbatim in aux_fhir_data.
        "note": [{"text": "patient reported palpitations"}],
    }
    r = api_client.post("/FHIR/R5/Observation", resource)
    assert r.status_code == 201, r.text

    obs = Observation.objects.get(subject_patient=patient)
    assert obs.aux_fhir_data["note"] == [{"text": "patient reported palpitations"}]

    # The unmapped field round-trips back out on read.
    read = api_client.get(f"/FHIR/R5/Observation/{obs.id}").json()
    assert read["note"] == [{"text": "patient reported palpitations"}]


def test_observation_omh_create_maps_columns_and_hides_criteria(api_client, device, hr_study, patient):
    record = generate_observation_value_attachment_data(Code.HeartRate.value)
    resource = {
        "resourceType": "Observation",
        "status": "final",
        "code": {"coding": [{"system": Code.OpenMHealth.value, "code": Code.HeartRate.value}]},
        "subject": {"reference": f"Patient/{patient.id}"},
        "device": {"reference": f"Device/{device.id}"},
        "valueAttachment": {
            "contentType": "application/json",
            "data": base64.b64encode(json.dumps(record).encode()).decode(),
        },
    }
    assert api_client.post("/FHIR/R5/Observation", resource).status_code == 201

    obs = Observation.objects.get(subject_patient=patient)
    # OMH code -> mapped onto columns, not aux.
    assert obs.codeable_concept is not None
    assert obs.omh_data["body"] == record["body"]

    read = api_client.get(f"/FHIR/R5/Observation/{obs.id}").json()
    assert read["code"]["coding"][0]["system"] == Code.OpenMHealth.value
    # The __criteria annotation never leaks into the rendered resource.
    assert "__criteria" not in read.get("meta", {})
    assert "__criteria" not in json.dumps(read)


def _non_omh_observation(patient_id, payload):
    return {
        "resourceType": "Observation",
        "status": "final",
        "code": {"coding": [{"system": "http://loinc.org", "code": "85354-9", "display": "Blood pressure panel"}]},
        "subject": {"reference": f"Patient/{patient_id}"},
        "valueAttachment": {
            "contentType": "application/json",
            "data": base64.b64encode(json.dumps(payload).encode()).decode(),
        },
    }


def test_observation_non_omh_create_goes_to_aux(api_client, patient):
    # A non-OMH (LOINC) observation: mapping bypassed, no device, no scope consent needed.
    payload = {"systolic": 120, "diastolic": 80}
    resource = _non_omh_observation(patient.id, payload)
    r = api_client.post("/FHIR/R5/Observation", resource)
    assert r.status_code == 201, r.text

    obs = Observation.objects.get(subject_patient=patient)
    # The OMH columns stay null; the clinical payload lives opaquely in aux_fhir_data.
    assert obs.codeable_concept is None
    assert obs.omh_data is None
    assert obs.aux_fhir_data["code"]["coding"][0]["system"] == "http://loinc.org"
    assert obs.aux_fhir_data["valueAttachment"]["data"] == resource["valueAttachment"]["data"]
    # subject/meta are structural and not duplicated into aux.
    assert "subject" not in obs.aux_fhir_data
    assert "meta" not in obs.aux_fhir_data

    # Read returns the code and value verbatim from aux (value NOT double-encoded).
    read = api_client.get(f"/FHIR/R5/Observation/{obs.id}").json()
    assert read["code"]["coding"][0]["code"] == "85354-9"
    assert read["valueAttachment"]["data"] == resource["valueAttachment"]["data"]
    assert read["subject"] == {"reference": f"Patient/{patient.id}"}


def test_observation_non_omh_skips_scope_consent(api_client, patient):
    # The patient has consented to no scopes, yet a non-OMH observation is accepted
    # (an OMH code without consent would be rejected with 403).
    assert not patient.consolidated_consented_scopes().exists()
    r = api_client.post("/FHIR/R5/Observation", _non_omh_observation(patient.id, {"x": 1}))
    assert r.status_code == 201, r.text


def test_patient_fhir_create_splits_columns_and_aux(organization, user):
    # Patient is read/search-only via FHIR (see __interaction), but the reverse-mapping
    # create logic is still exercised here at the model level.
    user.practitioner_profile.save_setting("current_organization_id", organization.id)
    resource = {
        "resourceType": "Patient",
        "name": [{"family": "Doe", "given": ["Jane"]}],
        "birthDate": "1990-07-15",
        "telecom": [{"system": "email", "value": "jane-fhir@example.com"}],
        "identifier": [{"system": "http://hospital.org", "value": "MRN-9"}],
        # Not in the mapping -> aux_fhir_data.
        "gender": "female",
    }
    patient = Patient.fhir_create(resource, user)
    # Config-mapped fields landed on columns...
    assert patient.name_family == "Doe"
    assert patient.name_given == "Jane"
    assert str(patient.birth_date) == "1990-07-15"
    # ...the linked JheUser was resolved from telecom email...
    assert patient.jhe_user.email == "jane-fhir@example.com"
    # ...identifiers fanned out to rows...
    assert patient.identifiers.get().value == "MRN-9"
    # ...and the unmapped field went to aux_fhir_data.
    assert patient.aux_fhir_data == {"gender": "female"}

    # The rendered resource reflects both column-mapped and aux fields.
    rendered = FHIRPatientSerializer().to_representation(patient)
    assert rendered["gender"] == "female"
    assert rendered["birthDate"] == "1990-07-15"


def test_patient_fhir_create_requires_practitioner(patient):
    with pytest.raises(PermissionDenied):
        Patient.fhir_create({"resourceType": "Patient", "birthDate": "2000-01-01"}, patient.jhe_user)


# ---------------------------------------------------------------------------
# __interaction allow-list enforcement
# ---------------------------------------------------------------------------


def test_interaction_create_on_read_only_resource_returns_405(api_client, patient):
    # Patient declares __interaction: [read, search]; create is refused before any handler.
    r = api_client.post("/FHIR/R5/Patient", {"resourceType": "Patient", "birthDate": "2000-01-01"})
    assert r.status_code == 405, r.text
    assert r.json()["resourceType"] == "OperationOutcome"


def test_interaction_update_and_delete_on_read_only_resource_return_405(api_client, patient):
    assert api_client.put(f"/FHIR/R5/Patient/{patient.id}", {"resourceType": "Patient"}).status_code == 405
    assert api_client.patch(f"/FHIR/R5/Patient/{patient.id}", {"resourceType": "Patient"}).status_code == 405
    assert api_client.delete(f"/FHIR/R5/Patient/{patient.id}").status_code == 405


def test_interaction_read_only_resource_allows_read(api_client, patient):
    # read is in the allow-list, so it is not blocked by interaction enforcement.
    assert api_client.get(f"/FHIR/R5/Patient/{patient.id}").status_code == 200


def test_interaction_disallowed_precedes_missing_handler(api_client):
    # Organization allows only read/search, so create is 405 even though it has no handler.
    assert api_client.post("/FHIR/R5/Organization", {"resourceType": "Organization"}).status_code == 405


def test_configured_resource_without_handler_returns_501(api_client):
    # Organization read IS allowed by __interaction, but no handler is wired up yet.
    r = api_client.get("/FHIR/R5/Organization/1")
    assert r.status_code == 501, r.text
    assert r.json()["resourceType"] == "OperationOutcome"


def test_observation_without_interaction_allows_all(api_client, device, hr_study, patient):
    # Observation declares no __interaction, so create (and the rest) stay available.
    record = generate_observation_value_attachment_data(Code.HeartRate.value)
    resource = {
        "resourceType": "Observation",
        "status": "final",
        "code": {"coding": [{"system": Code.OpenMHealth.value, "code": Code.HeartRate.value}]},
        "subject": {"reference": f"Patient/{patient.id}"},
        "device": {"reference": f"Device/{device.id}"},
        "valueAttachment": {
            "contentType": "application/json",
            "data": base64.b64encode(json.dumps(record).encode()).decode(),
        },
    }
    assert api_client.post("/FHIR/R5/Observation", resource).status_code == 201


# ---------------------------------------------------------------------------
# Backward-compatible lowercase /fhir/r5/ alias
# ---------------------------------------------------------------------------


def test_legacy_lowercase_path_alias_still_works(api_client, patient):
    # The canonical base is /FHIR/R5/; the old /fhir/r5/ path is kept as an alias.
    cid = api_client.post("/FHIR/R5/Condition", _condition(patient.id, code={"text": "x"})).json()["id"]
    r = api_client.get(f"/fhir/r5/Condition/{cid}")
    assert r.status_code == 200, r.text
    assert r.json()["id"] == str(cid)
