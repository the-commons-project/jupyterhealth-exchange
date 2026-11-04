"""The delivered document Bundle, read back the way a consumer would."""

import base64
import hashlib
import json

from patient_access.core.models import DocumentRecord, DocumentResult, DocumentSource, OutcomeStatus, SkipReason
from patient_access.core.settings import BundleSettings
from patient_access.formatters import render_bundle
from patient_access.formatters.document_bundle import DOCUMENTS_SECTION_CODE, urn
from patient_access.formatters.metadata import pull_metadata
from patient_access.testing.memory import MemorySpoolHandle

from .helpers import ISS, NOW, job

NOTE = "Patient seen today. été ✓".encode()
PDF = bytes(range(256)) * 300


def spool_with_records():
    spool = MemorySpoolHandle("job-1")
    spool.append("Patient", {"resourceType": "Patient", "id": "p1", "name": [{"family": "Zoë"}]})
    spool.append(
        "Observation",
        {"resourceType": "Observation", "id": "o1", "subject": {"reference": "Patient/p1"}, "status": "final"},
    )
    spool.append(
        "Observation",
        {
            "resourceType": "Observation",
            "id": "o2",
            "subject": {"reference": f"{ISS}/Patient/p1"},
            "performer": [{"reference": "Practitioner/dr1"}],
        },
    )
    spool.append(
        "DocumentReference",
        {
            "resourceType": "DocumentReference",
            "id": "d1",
            "subject": {"reference": "Patient/p1"},
            "content": [
                {"attachment": {"contentType": "application/pdf", "url": "Binary/pdf1"}},
                {"attachment": {"contentType": "text/plain", "url": "Binary/txt1"}},
            ],
        },
    )
    spool.append(
        "DiagnosticReport",
        {"resourceType": "DiagnosticReport", "id": "r1", "presentedForm": [{"contentType": "application/pdf"}]},
    )
    for blob_id, data, ctype, ref in (
        ("DocumentReference-d1-1", NOTE, "text/plain", "DocumentReference/d1"),
        ("DiagnosticReport-r1-0", PDF, "application/pdf", "DiagnosticReport/r1"),
    ):
        w = spool.open_blob(blob_id)
        w.write(data)
        w.commit(content_type=ctype, referenced_by=ref, source_url=None)
    return spool


def finished_job():
    j = job()
    for o in j.outcomes:
        if o.status is OutcomeStatus.PENDING:
            o.status = OutcomeStatus.OK
    j.outcomes[0].count = 1
    j.documents.status = OutcomeStatus.OK
    j.documents.record(
        DocumentRecord(
            blob_id="DocumentReference-d1-1",
            source=DocumentSource.DOCUMENT_REFERENCE,
            referenced_by="DocumentReference/d1",
            attachment_index=1,
            content_type="text/plain",
            result=DocumentResult.STORED,
            size=len(NOTE),
            sha256=hashlib.sha256(NOTE).hexdigest(),
        )
    )
    j.documents.record(
        DocumentRecord(
            blob_id="DiagnosticReport-r1-1",
            source=DocumentSource.DIAGNOSTIC_REPORT,
            referenced_by="DiagnosticReport/r1",
            attachment_index=1,
            content_type="application/pdf",
            result=DocumentResult.SKIPPED,
            reason=SkipReason.SIZE_CAP_EACH,
        )
    )
    return j


def render(j, spool, base="https://jupyterhealth.org/fhir/StructureDefinition/"):
    return json.loads(b"".join(render_bundle(j, spool, BundleSettings(extension_base_url=base), NOW)))


def test_bundle_is_a_document_with_the_composition_first():
    bundle = render(finished_job(), spool_with_records())
    assert bundle["resourceType"] == "Bundle" and bundle["type"] == "document"
    types = [e["resource"]["resourceType"] for e in bundle["entry"]]
    assert types[:2] == ["Composition", "Device"]
    assert sorted(types[2:]) == sorted(
        ["Patient", "Observation", "Observation", "DocumentReference", "DiagnosticReport"]
    )
    assert all(e["fullUrl"].startswith("urn:uuid:") for e in bundle["entry"])


def test_references_inside_the_pull_point_at_urns_and_the_rest_are_absolute():
    bundle = render(finished_job(), spool_with_records())
    by_id = {e["resource"].get("id"): e["resource"] for e in bundle["entry"]}
    patient_urn = urn("Patient", "p1")
    assert by_id["o1"]["subject"]["reference"] == patient_urn
    assert by_id["o2"]["subject"]["reference"] == patient_urn  # absolute on the server, still in the pull
    assert by_id["o2"]["performer"][0]["reference"] == f"{ISS}/Practitioner/dr1"
    composition = bundle["entry"][0]["resource"]
    assert composition["subject"]["reference"] == patient_urn


def test_documents_are_inline_and_decode_to_the_original_bytes():
    bundle = render(finished_job(), spool_with_records())
    by_id = {e["resource"].get("id"): e["resource"] for e in bundle["entry"]}
    content = by_id["d1"]["content"]
    assert "data" not in content[0]["attachment"]  # the format not chosen keeps its URL only
    note = content[1]["attachment"]
    assert base64.b64decode(note["data"]) == NOTE
    assert note["size"] == len(NOTE) and note["url"] == f"{ISS}/Binary/txt1"
    assert base64.b64decode(by_id["r1"]["presentedForm"][0]["data"]) == PDF


def test_provenance_reads_back():
    meta = pull_metadata(render(finished_job(), spool_with_records()))
    assert meta.job_id == "job-1" and meta.participant_ref == "40001"
    assert meta.participant_attrs["fhir_source_id"] == 7
    assert meta.source_brand == "Example Health"
    assert "patient/Observation.read" in meta.scopes_granted
    assert meta.scopes_declined == ("patient/Condition.read",)
    assert meta.meta_source == "https://jhe.example.org/fhir-source/7"
    labs = next(o for o in meta.outcomes if o.label == "Labs")
    assert labs.status is OutcomeStatus.OK and labs.resource_type == "Observation"
    conditions = next(o for o in meta.outcomes if o.label == "Conditions")
    assert conditions.status is OutcomeStatus.NOT_REQUESTED
    stored = [d for d in meta.documents if d.result is DocumentResult.STORED]
    skipped = [d for d in meta.documents if d.result is DocumentResult.SKIPPED]
    assert [d.referenced_by for d in stored] == ["DocumentReference/d1"]
    assert skipped[0].reason is SkipReason.SIZE_CAP_EACH


def test_sections_carry_outcomes_and_empty_reasons():
    composition = render(finished_job(), spool_with_records())["entry"][0]["resource"]
    sections = {s["code"]["text"]: s for s in composition["section"]}
    assert len(sections["Observation"]["entry"]) == 2
    assert len(sections["Observation"]["extension"]) == 2  # Labs and Vital Signs
    assert sections["Condition"]["emptyReason"]["coding"][0]["code"] == "withheld"
    docs = sections[DOCUMENTS_SECTION_CODE]
    assert docs["entry"] == [{"reference": urn("DocumentReference", "d1")}]


def test_the_extension_base_url_is_the_sites_setting():
    composition = render(finished_job(), spool_with_records(), base="https://example.edu/fhir/sd/")["entry"][0][
        "resource"
    ]
    assert composition["extension"][0]["url"] == "https://example.edu/fhir/sd/patient-access-pull"
    assert pull_metadata(render(finished_job(), spool_with_records(), base="https://example.edu/fhir/sd/")).job_id


def test_rendering_is_deterministic_apart_from_nothing():
    spool = spool_with_records()
    first = b"".join(render_bundle(finished_job(), spool, BundleSettings(), NOW))
    second = b"".join(render_bundle(finished_job(), spool, BundleSettings(), NOW))
    assert first == second


def test_resources_without_ids_still_get_urns_and_sections():
    spool = MemorySpoolHandle("j")
    spool.append("Condition", {"resourceType": "Condition"})
    spool.append("Condition", {"resourceType": "Condition"})
    bundle = render(job(), spool)
    urls = [e["fullUrl"] for e in bundle["entry"][2:]]
    assert len(set(urls)) == 2
