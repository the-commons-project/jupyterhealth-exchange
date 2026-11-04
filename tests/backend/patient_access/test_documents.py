"""The documents pass: preference, budget, the streaming downloader against a fake server, the
per-document records and resume."""

import base64
import hashlib
import json
import logging

import pytest
import responses

from patient_access.core.client import FhirClient
from patient_access.core.documents.budget import Budget
from patient_access.core.documents.candidates import Candidate, candidates, preferred
from patient_access.core.documents.downloader import Skipped, Stored, fetch_one
from patient_access.core.documents.run import fetch_documents
from patient_access.core.log import JobLog
from patient_access.core.models import DocumentResult, DocumentSource, OutcomeStatus, SkipReason
from patient_access.core.settings import DocumentCaps
from patient_access.core.source_settings import DocumentProfile
from patient_access.testing.memory import MemorySpoolHandle

from .helpers import ISS, NOW, Clock, job, source_settings

LOG = JobLog(logging.getLogger("test"), {"job_id": "j"})


def client(expires_in_minutes: float = 60) -> FhirClient:
    from datetime import timedelta

    from pydantic import SecretStr

    return FhirClient(
        ISS, SecretStr("tok"), source_settings().fetch, NOW + timedelta(minutes=expires_in_minutes), Clock()
    )


def cand(**kw) -> Candidate:
    values = {
        "blob_id": "DocumentReference-d1-0",
        "source": DocumentSource.DOCUMENT_REFERENCE,
        "referenced_by": "DocumentReference/d1",
        "attachment_index": 0,
        "content_type": "text/plain",
        "url": "Binary/b1",
    } | kw
    return Candidate(**values)


# --- pure pieces --------------------------------------------------------------------------


def test_preferred_takes_the_first_in_preference_order_then_the_earliest():
    prefer = ("text/plain", "text/html", "application/pdf")
    assert preferred(["application/pdf", "text/html; charset=utf-8", "text/plain"], prefer) == 2
    assert preferred(["image/tiff", "application/pdf"], prefer) == 1
    assert preferred(["image/tiff", "image/png"], prefer) == 0
    assert preferred([], prefer) is None


def test_budget_refuses_by_count_each_and_total():
    caps = DocumentCaps(max_count=2, max_bytes_each=10, max_total_bytes=15)
    b = Budget(caps=caps)
    assert b.refusal(10) is None and b.limit == 10
    assert b.refusal(11) is SkipReason.SIZE_CAP_EACH
    b = b.spend(8)
    assert b.refusal(8) is SkipReason.SIZE_CAP_TOTAL and b.limit == 7
    b = b.spend(1)
    assert b.refusal() is SkipReason.COUNT_CAP


def test_candidates_take_one_format_per_document_reference_and_every_rendering():
    spool = MemorySpoolHandle("j")
    spool.append(
        "DocumentReference",
        {
            "resourceType": "DocumentReference",
            "id": "d1",
            "content": [
                {"attachment": {"contentType": "application/pdf", "url": "Binary/1"}},
                {"attachment": {"contentType": "text/plain", "url": "Binary/2"}},
            ],
        },
    )
    spool.append(
        "DiagnosticReport",
        {"resourceType": "DiagnosticReport", "id": "r1", "presentedForm": [{"data": "eA=="}, {"url": "Binary/3"}]},
    )
    found = list(candidates(spool, DocumentProfile()))
    assert [c.blob_id for c in found] == ["DiagnosticReport-r1-0", "DiagnosticReport-r1-1", "DocumentReference-d1-1"]
    assert found[0].data == "eA==" and found[2].url == "Binary/2"


# --- the downloader ------------------------------------------------------------------------

BIG = DocumentCaps()


@responses.activate
def test_raw_bytes_are_streamed_into_the_spool():
    body = bytes(range(256)) * 1000
    responses.add(responses.GET, f"{ISS}/Binary/b1", body=body, content_type="application/pdf")
    spool = MemorySpoolHandle("j")
    result = fetch_one(client(), spool, cand(content_type="application/pdf"), Budget(caps=BIG), 1000)
    assert isinstance(result, Stored)
    assert result.meta.size == len(body) and result.meta.sha256 == hashlib.sha256(body).hexdigest()
    assert result.meta.content_type == "application/pdf"
    assert b"".join(spool.iter_blob("DocumentReference-d1-0")) == body
    assert responses.calls[0].request.headers["Accept"] == "application/pdf"


@responses.activate
def test_declared_size_over_the_cap_is_skipped_without_reading():
    responses.add(responses.GET, f"{ISS}/Binary/b1", body=b"x" * 50, headers={"Content-Length": "50"})
    spool = MemorySpoolHandle("j")
    result = fetch_one(client(), spool, cand(), Budget(caps=DocumentCaps(max_bytes_each=10)), 1000)
    assert result == Skipped(reason=SkipReason.SIZE_CAP_EACH)
    assert not spool.has_blob("DocumentReference-d1-0")


@responses.activate
def test_over_the_cap_mid_stream_discards_the_partial_blob():
    responses.add(responses.GET, f"{ISS}/Binary/b1", body=b"x" * 200_000)
    spool = MemorySpoolHandle("j")
    result = fetch_one(client(), spool, cand(), Budget(caps=DocumentCaps(max_total_bytes=100_000)), 1000)
    assert result == Skipped(reason=SkipReason.SIZE_CAP_TOTAL)
    assert not spool.has_blob("DocumentReference-d1-0")


def test_a_url_on_another_host_is_never_fetched():
    result = fetch_one(client(), MemorySpoolHandle("j"), cand(url="https://evil.example.com/x"), Budget(caps=BIG), 1)
    assert result == Skipped(reason=SkipReason.URL_OUTSIDE_SERVER)


@responses.activate
def test_a_binary_json_answer_is_unwrapped():
    payload = b"%PDF-1.7 hello"
    binary = {"resourceType": "Binary", "contentType": "application/pdf", "data": base64.b64encode(payload).decode()}
    responses.add(responses.GET, f"{ISS}/Binary/b1", json=binary, content_type="application/fhir+json")
    spool = MemorySpoolHandle("j")
    result = fetch_one(client(), spool, cand(content_type=None), Budget(caps=BIG), 10_000)
    assert isinstance(result, Stored) and result.meta.content_type == "application/pdf"
    assert b"".join(spool.iter_blob("DocumentReference-d1-0")) == payload


@responses.activate
def test_a_binary_json_answer_over_its_own_cap_is_skipped():
    binary = {"resourceType": "Binary", "data": base64.b64encode(b"x" * 5000).decode()}
    responses.add(responses.GET, f"{ISS}/Binary/b1", body=json.dumps(binary), content_type="application/fhir+json")
    result = fetch_one(client(), MemorySpoolHandle("j"), cand(), Budget(caps=BIG), 1000)
    assert result == Skipped(reason=SkipReason.BINARY_JSON_CAP)


@responses.activate
def test_http_errors_and_bad_inline_data_are_recorded_not_raised():
    responses.add(responses.GET, f"{ISS}/Binary/b1", status=404)
    assert fetch_one(client(), MemorySpoolHandle("j"), cand(), Budget(caps=BIG), 1) == Skipped(
        reason=SkipReason.HTTP_ERROR, detail="404"
    )
    bad = cand(url=None, data="not base64!!")
    assert fetch_one(client(), MemorySpoolHandle("j"), bad, Budget(caps=BIG), 1) == Skipped(
        reason=SkipReason.BAD_INLINE_DATA
    )
    assert fetch_one(client(), MemorySpoolHandle("j"), cand(url=None), Budget(caps=BIG), 1) == Skipped(
        reason=SkipReason.NO_CONTENT
    )


def test_inline_data_goes_through_the_same_writer():
    data = bytes(range(256)) * 700
    spool = MemorySpoolHandle("j")
    result = fetch_one(client(), spool, cand(url=None, data=base64.b64encode(data).decode()), Budget(caps=BIG), 1)
    assert isinstance(result, Stored) and result.meta.size == len(data)
    assert b"".join(spool.iter_blob("DocumentReference-d1-0")) == data


# --- the pass ------------------------------------------------------------------------------


def docs_spool(n: int) -> MemorySpoolHandle:
    spool = MemorySpoolHandle("j")
    for i in range(n):
        spool.append(
            "DocumentReference",
            {"resourceType": "DocumentReference", "id": f"d{i}", "content": [{"attachment": {"url": f"Binary/{i}"}}]},
        )
    return spool


@responses.activate
def test_every_candidate_gets_one_record_and_counts_are_derived():
    for i in range(3):
        responses.add(responses.GET, f"{ISS}/Binary/{i}", body=b"x" * (i + 1), content_type="text/plain")
    responses.add(responses.GET, f"{ISS}/Binary/3", status=503)
    j = job()
    fetch_documents(j, docs_spool(4), DocumentProfile(), DocumentCaps(max_count=10), client(), lambda: None, LOG)
    recs = {r.referenced_by: r for r in j.documents.records}
    assert len(recs) == 4
    assert [recs[f"DocumentReference/d{i}"].result for i in range(3)] == [DocumentResult.STORED] * 3
    assert recs["DocumentReference/d3"].reason is SkipReason.HTTP_ERROR
    assert len(j.documents.stored) == 3 and j.documents.bytes == 6
    assert j.documents.status is OutcomeStatus.OK


@responses.activate
def test_resume_skips_documents_already_stored():
    responses.add(responses.GET, f"{ISS}/Binary/0", body=b"zero", content_type="text/plain")
    responses.add(responses.GET, f"{ISS}/Binary/1", body=b"one", content_type="text/plain")
    j, spool = job(), docs_spool(2)
    fetch_documents(j, spool, DocumentProfile(), DocumentCaps(), client(), lambda: None, LOG)
    assert len(responses.calls) == 2
    fetch_documents(j, spool, DocumentProfile(), DocumentCaps(), client(), lambda: None, LOG)
    assert len(responses.calls) == 2  # nothing refetched
    assert len(j.documents.stored) == 2


@responses.activate
def test_count_cap_and_token_expiry_are_records_too():
    responses.add(responses.GET, f"{ISS}/Binary/0", body=b"zero", content_type="text/plain")
    j = job()
    fetch_documents(j, docs_spool(2), DocumentProfile(), DocumentCaps(max_count=1), client(), lambda: None, LOG)
    assert j.documents.skipped[SkipReason.COUNT_CAP] == 1

    expired = client(expires_in_minutes=0)
    j = job()
    fetch_documents(j, docs_spool(3), DocumentProfile(), DocumentCaps(), expired, lambda: None, LOG)
    assert j.documents.skipped[SkipReason.TOKEN_EXPIRED] == 3
    assert j.documents.status is OutcomeStatus.ERROR


@pytest.mark.parametrize("profile_sources", [frozenset(), frozenset({DocumentSource.DIAGNOSTIC_REPORT})])
def test_a_profile_can_turn_sources_off(profile_sources):
    j = job()
    fetch_documents(
        j, docs_spool(2), DocumentProfile(sources=profile_sources), DocumentCaps(), client(), lambda: None, LOG
    )
    assert j.documents.records == []
