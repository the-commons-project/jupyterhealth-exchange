import pytest
from jhe_mcp.omh_registry import all_schema_ids, all_short_names, load_schema, lookup_code, short_name

# JHE's authoritative supported OMH codes, from
# core/management/commands/seed.py::seed_codeable_concepts(). omh-shim must
# vendor a schema for each; if JHE adds a code, this set should grow and the
# drift test below fails until omh-shim catches up.
JHE_SEEDED_SHORT_NAMES = frozenset(
    {
        "blood-glucose",
        "blood-pressure",
        "body-temperature",
        "heart-rate",
        "oxygen-saturation",
        "respiratory-rate",
        "rr-interval",
    }
)


def test_all_schema_ids_from_shim():
    ids = all_schema_ids()
    assert "omh:heart-rate:2.0" in ids
    assert "omh:blood-pressure:4.0" in ids
    assert "omh:blood-glucose:4.0" in ids
    assert len(ids) >= 10


def test_covers_jhe_seeded_codes():
    """omh-shim must serve every OMH code JHE seeds (guards version drift)."""
    missing = JHE_SEEDED_SHORT_NAMES - set(all_short_names())
    assert not missing, f"omh-shim is missing JHE-seeded OMH code(s): {sorted(missing)}"


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


def test_load_schema_wearable():
    schema = load_schema("omh:heart-rate:2.0")
    assert schema["type"] == "object"
    assert "heart_rate" in schema.get("properties", {})


def test_load_schema_clinical():
    schema = load_schema("omh:blood-pressure:4.0")
    assert schema["type"] == "object"
    assert "systolic_blood_pressure" in schema.get("properties", {})


def test_load_schema_unknown():
    with pytest.raises(KeyError):
        load_schema("omh:nonexistent:1.0")
