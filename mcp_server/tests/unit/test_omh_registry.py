import pytest
from jhe_mcp.omh_registry import all_schema_ids, all_short_names, load_schema, lookup_code, short_name


def test_all_schema_ids_includes_shim_and_local():
    ids = all_schema_ids()
    assert "omh:heart-rate:2.0" in ids
    assert "omh:blood-pressure:4.0" in ids
    assert "omh:blood-glucose:4.0" in ids
    assert len(ids) >= 10


def test_short_name_extraction():
    assert short_name("omh:heart-rate:2.0") == "heart-rate"
    assert short_name("omh:blood-pressure:4.0") == "blood-pressure"
    assert short_name("local:heart-rate-variability:1.0") == "heart-rate-variability"


def test_lookup_code_known():
    code = lookup_code("heart-rate")
    assert code == "https://w3id.org/openmhealth|omh:heart-rate:2.0"


def test_lookup_code_unknown():
    assert lookup_code("nonexistent") is None


def test_all_short_names():
    names = all_short_names()
    assert "heart-rate" in names
    assert "blood-pressure" in names
    assert "blood-glucose" in names
    assert sorted(names) == names


def test_load_schema_from_shim():
    schema = load_schema("omh:heart-rate:2.0")
    assert schema["type"] == "object"
    assert "heart_rate" in schema.get("properties", {})


def test_load_schema_from_local():
    schema = load_schema("omh:blood-pressure:4.0")
    assert schema["type"] == "object"


def test_load_schema_unknown():
    with pytest.raises(KeyError):
        load_schema("omh:nonexistent:1.0")
