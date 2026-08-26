"""Tests for FHIR config validation (core/fhir/config.validate_config)."""

import copy

import pytest

from core.fhir import config
from core.fhir.engine import matches_criteria


@pytest.fixture
def base_config():
    return copy.deepcopy(config.FHIR_CONFIG)


def _validate(monkeypatch, cfg):
    # Re-derive the indexed views from the patched config so validate_config sees the change.
    monkeypatch.setattr(config, "FHIR_CONFIG", cfg)
    mapped = {e["resourceType"]: {k: v for k, v in e.items() if k != "resourceType"} for e in cfg["mapped_resources"]}
    aux = {e["resourceType"]: e for e in cfg["aux_resources"]}
    monkeypatch.setattr(config, "_MAPPED_RESOURCES", mapped)
    monkeypatch.setattr(config, "_AUX_RESOURCES", aux)
    return config.validate_config()


def _mapped(cfg, resource_type):
    return next(e for e in cfg["mapped_resources"] if e["resourceType"] == resource_type)


def test_shipped_config_is_valid():
    assert config.validate_config() == []


def test_missing_interaction_is_reported(monkeypatch, base_config):
    _mapped(base_config, "Device")["meta"].pop("__interaction")
    errors = _validate(monkeypatch, base_config)
    assert any("Device" in e and "__interaction" in e for e in errors)


def test_invalid_interaction_token_is_reported(monkeypatch, base_config):
    _mapped(base_config, "Device")["meta"]["__interaction"] = ["read", "bogus"]
    errors = _validate(monkeypatch, base_config)
    assert any("invalid interaction 'bogus'" in e for e in errors)


def test_all_interactions_without_criteria_is_reported(monkeypatch, base_config):
    # Patient with __interaction ['*'] but no __criteria could never fall back to aux.
    _mapped(base_config, "Patient")["meta"]["__interaction"] = ["*"]
    errors = _validate(monkeypatch, base_config)
    assert any("Patient" in e and "__criteria" in e for e in errors)


def test_nonexistent_path_is_reported(monkeypatch, base_config):
    _mapped(base_config, "Device")["name"] = "DataSource.does_not_exist"
    errors = _validate(monkeypatch, base_config)
    assert any("does_not_exist" in e for e in errors)


def test_invalid_fhir_field_is_reported(monkeypatch, base_config):
    _mapped(base_config, "Device")["notAFhirField"] = "'x'"
    errors = _validate(monkeypatch, base_config)
    assert any("notAFhirField" in e for e in errors)


# ---------------------------------------------------------------------------
# __criteria evaluation (core/fhir/engine.matches_criteria)
# ---------------------------------------------------------------------------


def _obs(system, code="omh:heart-rate:2.0"):
    return {"resourceType": "Observation", "code": {"coding": [{"system": system, "code": code}]}}


def test_shipped_observation_criteria_accepts_omh_and_ieee():
    # JHE treats the OMH and IEEE 1752 namespaces interchangeably, so both route to the
    # mapped Observation model and anything else falls through to the aux store.
    criteria = config.mapped_criteria("Observation")
    assert matches_criteria(_obs("https://w3id.org/openmhealth"), criteria)
    assert matches_criteria(_obs("https://w3id.org/ieee1752", "ieee:time-in-bed:1.0"), criteria)
    assert not matches_criteria(_obs("http://loinc.org", "85354-9"), criteria)
    assert not matches_criteria({"resourceType": "Observation"}, criteria)


def test_criteria_comma_separated_values_or():
    criteria = "code=http://a.example|,http://b.example|x"
    assert matches_criteria(_obs("http://a.example", "anything"), criteria)
    assert matches_criteria(_obs("http://b.example", "x"), criteria)
    # b.example is only accepted with code 'x'.
    assert not matches_criteria(_obs("http://b.example", "y"), criteria)


def test_unrecognised_criteria_matches_so_data_is_never_diverted_silently():
    # A misconfigured criteria must take the mapped path, not silently divert to the aux blob.
    assert matches_criteria(_obs("http://loinc.org"), "unsupported=whatever")
    assert matches_criteria(_obs("http://loinc.org"), "code=")
