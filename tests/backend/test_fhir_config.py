"""Tests for FHIR config validation (core/fhir/config.validate_config)."""

import copy

import pytest

from core.fhir import config


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
