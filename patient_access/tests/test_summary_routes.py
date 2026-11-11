"""The generated C-CDA row: one request in several spellings, and the server's answer picks."""

import logging
from datetime import timedelta

import pytest
import responses
from pydantic import SecretStr

from patient_access.core.client import FhirClient
from patient_access.core.log import JobLog
from patient_access.core.models import OutcomeStatus
from patient_access.core.puller import fetch_resources, is_ccda, plan_outcomes
from patient_access.testing.memory import MemorySpoolHandle

from .helpers import ISS, NOW, Clock, job, source_settings

LOG = JobLog(logging.getLogger("test"), {"job_id": "j"})


@pytest.fixture(autouse=True)
def _job_log(caplog):
    caplog.set_level(logging.INFO, logger="test")


SCOPES = ("openid", "launch/patient", "patient/DocumentReference.read")
DOCREF = f"{ISS}/DocumentReference/$docref"
SEARCH = f"{ISS}/DocumentReference"
BY_TYPE = {"type": "http://loinc.org|34133-9", "patient": "p1"}
BY_CATEGORY = {"category": "summary-document", "patient": "p1"}
params = responses.matchers.query_param_matcher


def bundle(*resources):
    return {"resourceType": "Bundle", "entry": [{"resource": r} for r in resources]}


def note(i):
    return {
        "resourceType": "DocumentReference",
        "id": f"note{i}",
        "type": {"coding": [{"system": "http://loinc.org", "code": "11506-3"}]},
        "content": [{"attachment": {"contentType": "text/html", "url": f"Binary/n{i}"}}],
    }


def ccda(i, code="34133-9"):
    return {
        "resourceType": "DocumentReference",
        "id": f"ccda{i}",
        "type": {"coding": [{"system": "http://loinc.org", "code": code}]},
        "content": [
            {
                "attachment": {"contentType": "application/xml", "url": f"Binary/c{i}"},
                "format": {"code": "urn:hl7-org:sdwg:ccda-structuredBody:2.1"},
            }
        ],
    }


def run():
    settings = source_settings()
    j = job(outcomes=plan_outcomes(settings, SCOPES, SCOPES))
    j.source = j.source.model_copy(update={"scopes_granted": SCOPES})
    spool = MemorySpoolHandle("j")
    client = FhirClient(ISS, SecretStr("tok"), settings.fetch, NOW + timedelta(minutes=60), Clock())
    assert fetch_resources(j, spool, settings, client, lambda: None, LOG)
    by_label = {o.label: o for o in j.outcomes}
    return by_label["Summary Documents"], by_label["Documents"], spool


def passed_over(caplog):
    """The routes the job's log says were passed over, in order."""
    return [r.getMessage().split(": ", 1)[1] for r in caplog.records if "passed over" in r.getMessage()]


def called(url, query=None):
    return [
        c for c in responses.calls if c.request.url.split("?")[0] == url and (query is None or query in c.request.url)
    ]


def test_is_ccda_reads_the_document_not_the_vendor():
    assert is_ccda(ccda(1))
    assert is_ccda(ccda(1, code="11506-3"))  # an encounter summary: the format and the XML say so
    assert is_ccda({"resourceType": "DocumentReference", "type": {"coding": [{"code": "34133-9"}]}})
    assert is_ccda({"resourceType": "DocumentReference", "content": [{"attachment": {"contentType": "text/xml"}}]})
    assert not is_ccda(note(1))
    assert not is_ccda({"resourceType": "DocumentReference"})


@responses.activate
def test_the_first_route_that_answers_is_used_and_the_rest_are_not_asked(caplog):
    """The shape of an Epic pull: its own category answers, and nothing else is asked."""
    responses.add(responses.GET, SEARCH, match=[params({"patient": "p1"})], json=bundle(note(1)))
    responses.add(responses.GET, SEARCH, match=[params(BY_CATEGORY)], json=bundle(ccda(1), ccda(2, code="11506-3")))
    summary, documents, spool = run()
    assert (summary.status, summary.count) == (OutcomeStatus.OK, 2)
    assert summary.query == "DocumentReference?category=summary-document"
    assert passed_over(caplog) == []
    assert documents.count == 1
    assert [r["id"] for r in spool.iter_type("DocumentReference")] == ["note1", "ccda1", "ccda2"]
    assert not called(DOCREF) and not called(SEARCH, "34133-9")


@responses.activate
def test_a_route_that_errors_or_answers_with_other_documents_is_passed_over(caplog):
    """A server that ignores the category it does not know, has no $docref, and answers the type."""
    responses.add(responses.GET, SEARCH, match=[params({"patient": "p1"})], json=bundle(note(1)))
    responses.add(
        responses.GET,
        SEARCH,
        match=[params(BY_CATEGORY)],
        json=bundle(
            note(1), note(2), {"resourceType": "OperationOutcome", "issue": [{"details": {"text": "partial"}}]}
        ),
    )
    responses.add(
        responses.GET,
        DOCREF,
        status=404,
        json={"resourceType": "OperationOutcome", "issue": [{"diagnostics": "unknown operation"}]},
    )
    responses.add(responses.GET, SEARCH, match=[params(BY_TYPE)], json=bundle(ccda(1)))
    summary, _, spool = run()
    assert (summary.status, summary.count) == (OutcomeStatus.OK, 1)
    assert summary.query == "DocumentReference?type=http://loinc.org|34133-9"
    assert passed_over(caplog) == [
        "DocumentReference?category=summary-document passed over (nothing kept (server: partial))",
        "DocumentReference/$docref passed over (unknown operation)",  # the log keeps no digits
    ]
    # note2 came back from a filter the server ignored: it is not a summary, so it is not stored
    assert [r["id"] for r in spool.iter_type("DocumentReference")] == ["note1", "ccda1"]


@responses.activate
def test_the_operation_answers_when_the_category_does_not(caplog):
    """The shape of an Oracle Health pull: the category finds nothing, $docref generates the CCD."""
    responses.add(responses.GET, SEARCH, match=[params({"patient": "p1"})], json=bundle(note(1)))
    responses.add(responses.GET, SEARCH, match=[params(BY_CATEGORY)], json=bundle())
    responses.add(responses.GET, DOCREF, match=[params({"patient": "p1"})], json=bundle(ccda(1)))
    summary, _, spool = run()
    assert (summary.status, summary.count, summary.query) == (OutcomeStatus.OK, 1, "DocumentReference/$docref")
    assert passed_over(caplog) == ["DocumentReference?category=summary-document passed over (nothing kept)"]
    assert [r["id"] for r in spool.iter_type("DocumentReference")] == ["note1", "ccda1"]
    assert not called(SEARCH, "34133-9")


@responses.activate
def test_a_summary_already_among_the_clinical_notes_ends_the_search(caplog):
    """The shape of an eClinicalWorks pull: the C-CDA is a clinical note, already stored."""
    responses.add(responses.GET, SEARCH, match=[params({"patient": "p1"})], json=bundle(note(1), ccda(1)))
    responses.add(responses.GET, SEARCH, match=[params(BY_CATEGORY)], json=bundle())
    responses.add(responses.GET, DOCREF, status=404, json={})
    responses.add(responses.GET, SEARCH, match=[params(BY_TYPE)], json=bundle(ccda(1)))
    summary, documents, spool = run()
    assert (summary.status, summary.count) == (OutcomeStatus.OK, 0)  # found, and already in the bundle
    assert summary.query == "DocumentReference?type=http://loinc.org|34133-9"
    assert len(passed_over(caplog)) == 2
    assert documents.count == 2
    assert [r["id"] for r in spool.iter_type("DocumentReference")] == ["note1", "ccda1"]


@responses.activate
def test_no_route_answering_is_an_empty_row_not_a_failed_pull(caplog):
    responses.add(responses.GET, SEARCH, match=[params({"patient": "p1"})], json=bundle(note(1)))
    responses.add(responses.GET, SEARCH, match=[params(BY_CATEGORY)], json=bundle())
    responses.add(responses.GET, DOCREF, status=400, json={})
    # the type route is not mocked: a refused connection
    summary, documents, spool = run()
    assert (summary.status, summary.count, summary.error) == (OutcomeStatus.OK, 0, None)
    assert summary.query == "DocumentReference?category=summary-document"
    said = passed_over(caplog)
    assert [a.split(" passed over")[0] for a in said] == [
        "DocumentReference?category=summary-document",
        "DocumentReference/$docref",
        "DocumentReference?type=http://loinc.org|34133-9",
    ]
    assert "nothing kept" in said[0] and "network" in said[2]
    assert documents.status is OutcomeStatus.OK
    assert [r["id"] for r in spool.iter_type("DocumentReference")] == ["note1"]


@responses.activate
def test_a_route_the_server_ignored_is_not_paged_to_its_end(caplog):
    """A filter the server ignores returns the whole record, a page at a time."""
    responses.add(responses.GET, SEARCH, match=[params({"patient": "p1"})], json=bundle(note(1)))
    page = bundle(note(2), note(3)) | {"link": [{"relation": "next", "url": f"{SEARCH}?page=2"}]}
    responses.add(responses.GET, SEARCH, match=[params(BY_CATEGORY)], json=page)
    responses.add(responses.GET, DOCREF, match=[params({"patient": "p1"})], json=bundle(ccda(1)))
    summary, _, _ = run()
    assert (summary.count, summary.query) == (1, "DocumentReference/$docref")
    assert passed_over(caplog) == ["DocumentReference?category=summary-document passed over (nothing kept)"]
    assert not called(SEARCH, "page=2")
