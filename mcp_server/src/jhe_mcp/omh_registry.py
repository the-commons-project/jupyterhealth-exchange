from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

from omh_shim._schema_loader import known_ids as _shim_known_ids
from omh_shim._schema_loader import load as _shim_load

_OMH_SYSTEM = "https://w3id.org/openmhealth"

_LOCAL_SCHEMA_DIR = Path(__file__).resolve().parents[3] / "data" / "omh" / "json-schemas" / "data"

_LOCAL_SCHEMAS: dict[str, str] = {
    "omh:blood-pressure:4.0": "schema-omh_blood-pressure_4-0.json",
    "omh:blood-glucose:4.0": "schema-omh_blood-glucose_4-0.json",
    "omh:body-temperature:4.0": "schema-omh_body-temperature_4-0.json",
    "omh:respiratory-rate:2.0": "schema-omh_respiratory-rate_2-0.json",
    "omh:rr-interval:1.0": "schema-omh_rr-interval_1-0.json",
}


def all_schema_ids() -> frozenset[str]:
    return _shim_known_ids() | frozenset(_LOCAL_SCHEMAS)


@cache
def load_schema(schema_id: str) -> dict[str, Any]:
    if schema_id in _LOCAL_SCHEMAS:
        path = _LOCAL_SCHEMA_DIR / _LOCAL_SCHEMAS[schema_id]
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return _shim_load(schema_id)


def short_name(schema_id: str) -> str:
    parts = schema_id.split(":")
    return parts[1] if len(parts) >= 2 else schema_id


def lookup_code(data_type_short_name: str) -> str | None:
    for sid in all_schema_ids():
        if short_name(sid) == data_type_short_name:
            return f"{_OMH_SYSTEM}|{sid}"
    return None


def all_short_names() -> list[str]:
    return sorted(short_name(sid) for sid in all_schema_ids())
