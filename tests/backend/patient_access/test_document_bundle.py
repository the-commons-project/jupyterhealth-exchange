"""The delivered document Bundle, read back the way a consumer would."""

import base64
import hashlib
import json

import pytest

from patient_access.core.documents.candidates import blob_id
from patient_access.core.models import DocumentRecord, DocumentResult, DocumentSource, OutcomeStatus, SkipReason
from patient_access.core.settings import BundleSettings
from patient_access.core.vocab import TypeChoice
from patient_access.formatters import render_bundle
from patient_access.formatters.document_bundle import DOCUMENTS_SECTION_CODE, GENERATED_BY, binary_urn, urn
from patient_access.formatters.metadata import NotAPullBundle, pull_metadata
from patient_access.testing.memory import MemorySpoolHandle

from .helpers import ISS, NOW, job

NOTE = "Patient seen today. été ✓".encode()
PDF = bytes(range(256)) * 300
NOTE_ID = blob_id("DocumentReference", "d1", 1)
PDF_ID = blob_id("DiagnosticReport", "r1", 0)
REFUSED_ID = blob_id("DiagnosticReport", "r1", 1)


def spool_with_records(spool=None):
    spool = spool if spool is not None else MemorySpoolHandle("job-1")
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
        {
            "resourceType": "DiagnosticReport",
            "id": "r1",
            "presentedForm": [
                {"contentType": "application/pdf"},
                # Inline data the caps refused: must not reach the bundle anyway.
                {"contentType": "application/pdf", "data": base64.b64encode(b"x" * 500).decode(), "size": 500},
            ],
        },
    )
    for bid, data, ctype, ref in (
        (NOTE_ID, NOTE, "text/plain; charset=windows-1252", "DocumentReference/d1"),
        (PDF_ID, PDF, "application/pdf", "DiagnosticReport/r1"),
    ):
        w = spool.open_blob(bid)
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
    for bid, data, ref, source, index in (
        (NOTE_ID, NOTE, "DocumentReference/d1", DocumentSource.DOCUMENT_REFERENCE, 1),
        (PDF_ID, PDF, "DiagnosticReport/r1", DocumentSource.DIAGNOSTIC_REPORT, 0),
    ):
        j.documents.record(
            DocumentRecord(
                blob_id=bid,
                source=source,
                referenced_by=ref,
                attachment_index=index,
                content_type="text/plain",
                result=DocumentResult.STORED,
                size=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
            )
        )
    j.documents.record(
        DocumentRecord(
            blob_id=REFUSED_ID,
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


def test_bundle_is_a_document_with_the_composition_first_and_the_binaries_last():
    bundle = render(finished_job(), spool_with_records())
    assert bundle["resourceType"] == "Bundle" and bundle["type"] == "document"
    types = [e["resource"]["resourceType"] for e in bundle["entry"]]
    assert types[:2] == ["Composition", "Device"]
    assert types[-2:] == ["Binary", "Binary"]
    assert sorted(types[2:-2]) == sorted(
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


def test_a_reference_to_another_server_is_never_taken_for_one_of_ours():
    spool = spool_with_records()
    spool.append(
        "Observation",
        {
            "resourceType": "Observation",
            "id": "o3",
            "subject": {"reference": "https://other.example.org/fhir/Patient/p1"},
            "derivedFrom": [{"reference": "Observation/o1/_history/3"}],
        },
    )
    by_id = {e["resource"].get("id"): e["resource"] for e in render(finished_job(), spool)["entry"]}
    assert by_id["o3"]["subject"]["reference"] == "https://other.example.org/fhir/Patient/p1"
    assert by_id["o3"]["derivedFrom"][0]["reference"] == urn("Observation", "o1")  # version suffix dropped


def test_documents_are_binaries_the_attachments_point_at():
    bundle = render(finished_job(), spool_with_records())
    by_id = {e["resource"].get("id"): e["resource"] for e in bundle["entry"]}
    content = by_id["d1"]["content"]
    assert content[0]["attachment"]["url"] == f"{ISS}/Binary/pdf1"  # the format not taken keeps its URL
    note = content[1]["attachment"]
    assert note["url"] == binary_urn(NOTE_ID) and note["size"] == len(NOTE) and "data" not in note
    binary = next(e for e in bundle["entry"] if e["fullUrl"] == binary_urn(NOTE_ID))["resource"]
    assert binary["resourceType"] == "Binary" and base64.b64decode(binary["data"]) == NOTE
    assert binary["contentType"] == "text/plain; charset=windows-1252"  # the served charset is kept
    pdf = by_id["r1"]["presentedForm"][0]
    pdf_binary = next(e for e in bundle["entry"] if e["fullUrl"] == pdf["url"])["resource"]
    assert base64.b64decode(pdf_binary["data"]) == PDF


def test_no_attachment_carries_data_without_a_binary_behind_it():
    """The caps bound the bundle: inline bytes the EHR sent for a refused document are dropped."""
    bundle = render(finished_job(), spool_with_records())
    attachments = [
        *(c["attachment"] for e in bundle["entry"] for c in e["resource"].get("content", [])),
        *(f for e in bundle["entry"] for f in e["resource"].get("presentedForm", [])),
    ]
    assert attachments and all("data" not in a for a in attachments)
    refused = next(e for e in bundle["entry"] if e["resource"].get("id") == "r1")["resource"]["presentedForm"][1]
    assert refused["size"] == 500  # what the EHR said about it is kept


def test_an_inline_alternate_nobody_tried_is_delivered_as_the_ehr_sent_it():
    """Only attachments the pass tried and could not deliver lose their inline data. A format
    that was never a candidate (its sibling was stored) stays, bytes and all."""
    spool = spool_with_records()
    inline = base64.b64encode(b"%PDF-alt").decode()
    spool.append(
        "DocumentReference",
        {
            "resourceType": "DocumentReference",
            "id": "d2",
            "content": [
                {"attachment": {"contentType": "text/plain", "url": "Binary/t2"}},
                {"attachment": {"contentType": "application/pdf", "data": inline, "size": 8}},
            ],
        },
    )
    j = finished_job()
    tried = blob_id("DocumentReference", "d2", 0)
    j.documents.record(
        DocumentRecord(
            blob_id=tried,
            source=DocumentSource.DOCUMENT_REFERENCE,
            referenced_by="DocumentReference/d2",
            attachment_index=0,
            content_type="text/plain",
            result=DocumentResult.SKIPPED,
            reason=SkipReason.NETWORK,
        )
    )
    d2 = next(e for e in render(j, spool)["entry"] if e["resource"].get("id") == "d2")["resource"]
    assert d2["content"][0]["attachment"]["url"] == f"{ISS}/Binary/t2" and "data" not in d2["content"][0]["attachment"]
    assert d2["content"][1]["attachment"]["data"] == inline


def test_provenance_reads_back():
    meta = pull_metadata(render(finished_job(), spool_with_records()))
    assert meta.job_id == "job-1" and meta.participant_ref == "40001" and meta.finished == NOW.isoformat()
    assert meta.participant_attrs["fhir_source_id"] == 7
    assert meta.source_brand == "Example Health" and meta.type_choice == TypeChoice.HOST.value
    assert "patient/Observation.read" in meta.scopes_granted
    assert meta.scopes_declined == ("patient/Condition.read",)
    assert meta.meta_source == "https://jhe.example.org/fhir-source/7"
    labs = next(o for o in meta.outcomes if o.label == "Labs")
    assert labs.status is OutcomeStatus.OK and labs.resource_type == "Observation"
    conditions = next(o for o in meta.outcomes if o.label == "Conditions")
    assert conditions.status is OutcomeStatus.NOT_REQUESTED
    stored = [d for d in meta.documents if d.result is DocumentResult.STORED]
    skipped = [d for d in meta.documents if d.result is DocumentResult.SKIPPED]
    assert [d.referenced_by for d in stored] == ["DocumentReference/d1", "DiagnosticReport/r1"]
    assert stored[0].binary == binary_urn(NOTE_ID)
    assert skipped[0].reason is SkipReason.SIZE_CAP_EACH and skipped[0].binary is None


def test_the_pull_records_the_host_and_the_connection():
    settings = BundleSettings(host_name="jupyterhealth-exchange", host_version="2.3.0")
    raw = json.loads(b"".join(render_bundle(finished_job(), spool_with_records(), settings, NOW)))
    pull = raw["entry"][0]["resource"]["extension"][0]["extension"]
    software = {e["url"]: e.get("valueString") for e in next(p for p in pull if p["url"] == "software")["extension"]}
    assert software["hostName"] == "jupyterhealth-exchange" and software["hostVersion"] == "2.3.0"
    source = {e["url"]: e for e in next(p for p in pull if p["url"] == "source")["extension"]}
    assert source["pkceAdvertised"]["valueBoolean"] is True and source["typeChoice"]["valueString"] == "host"


def test_sections_carry_outcomes_and_empty_reasons():
    composition = render(finished_job(), spool_with_records())["entry"][0]["resource"]
    sections = {s["code"]["text"]: s for s in composition["section"]}
    assert len(sections["Observation"]["entry"]) == 2
    assert len(sections["Observation"]["extension"]) == 2  # Labs and Vital Signs
    assert sections["Condition"]["emptyReason"]["coding"][0]["code"] == "withheld"
    assert "text" not in sections["Observation"]  # a section with entries carries no generated text
    docs = sections[DOCUMENTS_SECTION_CODE]
    assert docs["entry"] == [{"reference": binary_urn(NOTE_ID)}, {"reference": binary_urn(PDF_ID)}]
    outcome = {e["url"]: e for e in docs["extension"][0]["extension"]}
    assert outcome["bytes"]["valueDecimal"] == len(NOTE) + len(PDF)  # a decimal: no 32-bit limit
    assert "seconds" in outcome and "capped" in outcome


def test_a_documents_pass_that_failed_reads_unavailable_not_none_known():
    j = job()
    j.documents.status, j.documents.error = OutcomeStatus.ERROR, "authorization ended"
    docs = render(j, MemorySpoolHandle("j"))["entry"][0]["resource"]["section"][-1]
    assert docs["emptyReason"]["coding"][0]["code"] == "unavailable"
    assert "did not return them" in docs["text"]["div"]


def test_the_documents_narrative_says_when_documents_were_offered_but_none_could_be_included():
    j = job()
    j.documents.status = OutcomeStatus.OK
    j.documents.record(
        DocumentRecord(
            blob_id=NOTE_ID,
            source=DocumentSource.DOCUMENT_REFERENCE,
            referenced_by="DocumentReference/d1",
            attachment_index=0,
            content_type="text/plain",
            result=DocumentResult.SKIPPED,
            reason=SkipReason.HTTP_ERROR,
            detail="503",
        )
    )
    docs = render(j, MemorySpoolHandle("j"))["entry"][0]["resource"]["section"][-1]
    assert docs["emptyReason"]["coding"][0]["code"] == "unavailable"
    assert "1 documents were offered and none could be included" in docs["text"]["div"]


def test_a_type_whose_scope_was_not_granted_at_all_reads_withheld():
    j = job()
    for o in j.outcomes:
        if o.label == "Conditions":
            o.status = OutcomeStatus.SKIPPED
    sections = {s["code"]["text"]: s for s in render(j, MemorySpoolHandle("j"))["entry"][0]["resource"]["section"]}
    assert sections["Condition"]["emptyReason"]["coding"][0]["code"] == "withheld"


def test_every_section_has_text_or_entries_and_generated_text_says_it_is_ours():
    """R4 cmp-1: a section needs text, entries or sub-sections. An empty section's narrative is
    the module's own statement, marked as generated and saying so in words, under the host's name."""
    settings = BundleSettings(
        extension_base_url="https://jupyterhealth.org/fhir/StructureDefinition/", host_name="Example Host"
    )
    bundle = json.loads(b"".join(render_bundle(finished_job(), spool_with_records(), settings, NOW)))
    sections = bundle["entry"][0]["resource"]["section"]
    assert all(("text" in s) != ("entry" in s) for s in sections)  # one or the other, never both
    empty = [s for s in sections if "entry" not in s]
    assert empty
    for s in empty:
        assert s["text"]["status"] == "generated"
        assert s["text"]["div"].startswith('<div xmlns="http://www.w3.org/1999/xhtml">')
        assert GENERATED_BY.format(host="Example Host") in s["text"]["div"]
        assert s["title"] in s["text"]["div"]
    condition = next(s for s in empty if s["code"]["text"] == "Condition")
    assert "not requested by the patient, or not granted" in condition["text"]["div"]


def test_the_generated_text_is_xhtml_safe():
    j = finished_job()
    j.source = j.source.model_copy(update={"brand_name": "<b>Ex&Co</b>"})
    settings = BundleSettings(
        extension_base_url="https://jupyterhealth.org/fhir/StructureDefinition/", host_name="A <Host> & Co"
    )
    bundle = json.loads(b"".join(render_bundle(j, spool_with_records(), settings, NOW)))
    div = next(s for s in bundle["entry"][0]["resource"]["section"] if "entry" not in s)["text"]["div"]
    assert "&lt;Host&gt; &amp; Co" in div and "<Host>" not in div


def test_reading_something_that_is_not_a_pull_bundle_says_so():
    with pytest.raises(NotAPullBundle):
        pull_metadata({"resourceType": "Bundle", "entry": [{"resource": {"resourceType": "Patient"}}]})
    with pytest.raises(NotAPullBundle):
        pull_metadata({"resourceType": "Bundle", "entry": [{"resource": {"resourceType": "Composition"}}]})


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


def test_every_resource_comes_out_of_the_disk_spool_as_it_went_in(tmp_path):
    """Over the real encrypted spool: no reference or attachment to rewrite, so equal exactly."""
    from patient_access.spool.local_disk import LocalDiskSpool

    spool = LocalDiskSpool(tmp_path).open("job-1", bytes(range(32)))
    resources = [
        {"resourceType": "Condition", "id": f"c{i}", "code": {"text": "Zoë ✓  "}, "onsetDateTime": "2020-01-01"}
        for i in range(50)
    ]
    for r in resources:
        spool.append("Condition", r)
    spool.flush("Condition")
    out = [e["resource"] for e in render(job(), spool)["entry"] if e["resource"]["resourceType"] == "Condition"]
    assert out == resources


def test_over_the_disk_spool_every_entry_is_a_resource_and_every_section_a_type(tmp_path):
    """The disk spool's own files (its document index) never surface as resources or sections."""
    from patient_access.spool.local_disk import LocalDiskSpool

    spool = spool_with_records(LocalDiskSpool(tmp_path).open("job-1", bytes(range(32))))
    bundle = render(finished_job(), spool)
    types = [e["resource"].get("resourceType") for e in bundle["entry"]]
    assert None not in types and types.count("Binary") == 2
    titles = {s["title"] for s in bundle["entry"][0]["resource"]["section"]}
    assert "blobs" not in titles and "index" not in titles
