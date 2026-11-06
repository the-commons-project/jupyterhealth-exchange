"""The standalone validator (scripts/validate_patient_access_bundle.py) against bundles the
formatter writes: a sound bundle passes, and each kind of damage is named. The validator reads
the file by the specification alone (docs/patient-access-bundle.md), independent of this code."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from patient_access.core.models import DocumentResult, OutcomeStatus
from patient_access.core.settings import BundleSettings
from patient_access.formatters import render_bundle

from .helpers import NOW
from .test_document_bundle import NOTE_ID, finished_job, spool_with_records

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "validate_patient_access_bundle.py"
spec = importlib.util.spec_from_file_location("validate_patient_access_bundle", SCRIPT)
assert spec is not None and spec.loader is not None
validator = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = validator  # dataclasses resolve annotations through sys.modules
spec.loader.exec_module(validator)

SETTINGS = BundleSettings(
    extension_base_url="https://jupyterhealth.org/fhir/StructureDefinition/", host_name="Example Host"
)


def consistent_job():
    """`finished_job` is a loose fixture for the formatter's own tests; a delivered bundle's
    outcomes agree with its entries, so here every row says what the spool holds."""
    j = finished_job()
    counts = {"Patient": 1, "DiagnosticReport": 1, "DocumentReference": 1, "Observation": 2}
    for o in j.outcomes:
        if o.resource_type in counts:
            n = counts[o.resource_type] if o.label != "Vital Signs" else 0  # both Observations count as labs
            o.status, o.count, o.error = OutcomeStatus.OK, n, None
    for r in list(j.documents.records):
        if r.result is DocumentResult.STORED:  # frozen values: replace the record, as the pass would
            served = "text/plain; charset=windows-1252" if r.blob_id == NOTE_ID else "application/pdf"
            j.documents.record(r.model_copy(update={"content_type": served}))
    return j


def rendered() -> bytes:
    return b"".join(render_bundle(consistent_job(), spool_with_records(), SETTINGS, NOW))


def _count_of(section: dict) -> dict:
    return next(e for e in section["extension"][0]["extension"] if e["url"] == "count")


def failures(path: Path) -> list[str]:
    return [name for ok, name, _ in validator.validate(path).checks if not ok]


def test_a_bundle_the_formatter_writes_passes_every_check(tmp_path):
    path = tmp_path / "bundle.json"
    path.write_bytes(rendered())
    report = validator.validate(path)
    assert report.failed == []
    assert ("Observation", 2, "ok,ok") in report.sections
    assert report.sections[-1][0] == "documents" and report.sections[-1][1] == 2


@pytest.mark.parametrize(
    ("damage", "named"),
    [
        (lambda b: b["entry"][0]["resource"]["section"][7]["entry"].pop(), "every resource is listed in a section"),
        (
            lambda b: b["entry"][0]["resource"]["section"][0]["entry"].append({"reference": b["entry"][3]["fullUrl"]}),
            "every reference resolves to a Patient",
        ),
        (lambda b: b["entry"][-1]["resource"].update(data="AAAA"), "consistent with its attachment and Binary"),
        (lambda b: b["entry"].append(dict(b["entry"][2])), "fullUrls are unique"),
        (lambda b: b["entry"][0]["resource"]["section"][1].pop("text"), "text xor entries"),
        (lambda b: b["entry"][2]["resource"].update(subject={"reference": "Patient/p1"}), "no reference is relative"),
        (
            lambda b: _count_of(b["entry"][0]["resource"]["section"][0]).update(valueInteger=5),
            "sum of its ok counts",
        ),
    ],
)
def test_each_kind_of_damage_is_named(tmp_path, damage, named):
    bundle = json.loads(rendered())
    damage(bundle)
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(bundle, ensure_ascii=True))
    assert any(named in f for f in failures(path)), failures(path)


def test_non_ascii_and_non_finite_numbers_are_refused(tmp_path):
    path = tmp_path / "bundle.json"
    path.write_bytes(rendered().replace(b'"status":"final"', '"status":"final","x":"é"'.encode()))
    assert "file is ASCII-only UTF-8" in failures(path)
    path.write_bytes(rendered().replace(b'"status":"final"', b'"status":"final","x":NaN'))
    assert "file is strict JSON" in failures(path)
