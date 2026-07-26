"""GET /FHIR/R5/metadata — the public capabilities interaction (issue #615)."""

import pytest
from rest_framework.test import APIClient

from core.fhir.fhir_validation import validate_fhir_resource

pytestmark = pytest.mark.django_db


def _fetch():
    return APIClient().get("/FHIR/R5/metadata")  # deliberately unauthenticated


def _resource(statement, resource_type):
    return next(r for r in statement["rest"][0]["resource"] if r["type"] == resource_type)


def test_metadata_is_public_and_valid():
    response = _fetch()
    assert response.status_code == 200
    body = response.json()
    assert body["resourceType"] == "CapabilityStatement"
    assert body["kind"] == "instance"
    assert body["fhirVersion"] == "5.0.0"
    assert body["format"] == ["json"]
    validate_fhir_resource("CapabilityStatement", body)  # fhir.resources parse = R5-valid


def test_observation_capabilities_reflect_config():
    observation = _resource(_fetch().json(), "Observation")
    codes = {i["code"] for i in observation["interaction"]}
    assert {"create", "read", "search-type"} <= codes
    params = {p["name"]: p["type"] for p in observation["searchParam"]}
    assert params["date"] == "date"
    assert params["code"] == "token"


def test_patient_capabilities_union_mapped_and_aux():
    patient = _resource(_fetch().json(), "Patient")
    assert {"read", "search-type"} <= {i["code"] for i in patient["interaction"]}
    params = {p["name"]: p["type"] for p in patient["searchParam"]}
    assert params["birthdate"] == "date"
    assert params["family"] == "string" and params["given"] == "string" and params["name"] == "string"
    assert "identifier" in params  # aux-only Patient param unioned in


def test_security_block_expectations_and_batch():
    body = _fetch().json()
    rest = body["rest"][0]
    assert rest["security"]["service"][0]["coding"][0]["code"] == "SMART-on-FHIR"
    uris = {e["url"]: e["valueUri"] for e in rest["security"]["extension"][0]["extension"]}
    assert uris["authorize"].startswith("http://testserver") and uris["authorize"].endswith("/o/authorize/")
    assert uris["token"].endswith("/o/token/")
    expectation = _resource(body, "Patient")["interaction"][0]["extension"][0]
    assert expectation["url"].endswith("capabilitystatement-expectation")
    assert expectation["valueCode"] == "SHALL"
    assert {"code": "batch"} in rest["interaction"]
