# core/utils.py
import json
import random
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from django.conf import settings
from django.utils import timezone
from jsonschema import validators
from referencing import Registry, Resource


class NoNetwork:
    def __call__(self, uri: str):
        raise RuntimeError(f"Remote $ref blocked (not preloaded): {uri}")


def _preload_dir(reg: Registry, base_url: str, directory: Path) -> Registry:
    for p in directory.glob("*.json"):
        url = base_url + p.name  # e.g. https://w3id.org/ieee/ieee-1752-schema/header-1.0.json
        reg = reg.with_resource(url, Resource.from_contents(json.loads(p.read_text())))
    return reg


def build_schema_registry() -> Registry:
    ieee_base = "https://w3id.org/ieee/ieee-1752-schema/"
    omh_base = "https://w3id.org/openmhealth/schemas/omh/"
    omh_utility = "https://opensource.ieee.org/omh/1752/-/raw/main/schemas/utility/"

    reg = Registry()
    reg = _preload_dir(reg, "", settings.DATA_DIR_PATH.schemas_metadata)
    reg = _preload_dir(reg, "", settings.DATA_DIR_PATH.schemas_utility)
    reg = _preload_dir(reg, ieee_base, settings.DATA_DIR_PATH.schemas_metadata)
    reg = _preload_dir(reg, ieee_base, settings.DATA_DIR_PATH.schemas_utility)
    reg = _preload_dir(reg, omh_base, settings.DATA_DIR_PATH.schemas_utility)
    reg = _preload_dir(reg, omh_utility, settings.DATA_DIR_PATH.schemas_utility)

    return reg


def validate_with_registry(*, instance, schema, forbid_unknown_network: bool = True):
    Validator = validators.validator_for(schema)
    Validator.check_schema(schema)

    preloaded = build_schema_registry()
    registry = preloaded.combine(Registry(retrieve=NoNetwork())) if forbid_unknown_network else preloaded
    Validator(schema, registry=registry).validate(instance)


def generate_observation_value_attachment_data(coding_code):
    data_point = settings.DATA_DIR_PATH.examples_data_point / (
        coding_code.replace(":", "_").replace(".", "-") + ".json"
    )
    if not data_point.exists():
        return "placeholder"

    placeholder = json.loads(data_point.read_text())

    placeholder.get("header")["uuid"] = str(uuid4())
    placeholder.get("header")["uuid"] = str(timezone.now())

    body = placeholder.get("body")
    for key in ("body_temperature", "oxygen_saturation", "respiratory_rate"):
        field = body.get(key)
        if field and "value" in field:
            field["value"] += random.randint(1, 10)

    body["effective_time_frame"] = {"date_time": str(timezone.now() + timedelta(hours=1))}
    return placeholder
