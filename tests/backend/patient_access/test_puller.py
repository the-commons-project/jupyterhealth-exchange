"""Phase 4's resource walk: paging, per-type isolation, dedupe, expiry, the 401 stop."""

import logging
from datetime import timedelta

import pytest
import responses
from pydantic import SecretStr

from patient_access.core.client import FhirClient, UrlRefusal
from patient_access.core.log import JobLog
from patient_access.core.models import OutcomeStatus
from patient_access.core.puller import FetchAborted, fetch_resources, plan_outcomes
from patient_access.testing.memory import MemorySpoolHandle

from .helpers import ISS, NOW, Clock, job, source_settings

LOG = JobLog(logging.getLogger("test"), {"job_id": "j"})


def make_client(clock=None, minutes=60):
    return FhirClient(
        ISS, SecretStr("tok"), source_settings().fetch, NOW + timedelta(minutes=minutes), clock or Clock()
    )


def search(resources, next_url=None):
    body = {"resourceType": "Bundle", "entry": [{"resource": r} for r in resources]}
    if next_url:
        body["link"] = [{"relation": "next", "url": next_url}]
    return body


def obs(i, category="laboratory"):
    return {"resourceType": "Observation", "id": f"o{i}", "category": [{"text": category}]}


def test_plan_outcomes_marks_what_was_withheld():
    outcomes = {
        o.label: o.status
        for o in plan_outcomes(
            source_settings(),
            requested=("patient/Observation.read", "patient/Condition.read"),
            granted=("patient/Observation.rs",),
        )
    }
    assert outcomes["Labs"] is OutcomeStatus.PENDING  # granted in v2 form still counts
    assert outcomes["Conditions"] is OutcomeStatus.NOT_GRANTED
    assert outcomes["Allergies"] is OutcomeStatus.NOT_REQUESTED


@responses.activate
def test_pages_are_followed_and_duplicates_across_rows_dropped():
    responses.add(responses.GET, f"{ISS}/Patient/p1", json={"resourceType": "Patient", "id": "p1"})
    responses.add(
        responses.GET,
        f"{ISS}/Observation",
        match=[responses.matchers.query_param_matcher({"category": "laboratory", "patient": "p1"})],
        json=search([obs(1), obs(2)], next_url=f"{ISS}/Observation?page=2"),
    )
    responses.add(
        responses.GET,
        f"{ISS}/Observation",
        match=[responses.matchers.query_param_matcher({"page": "2"})],
        json=search([obs(3), {"resourceType": "OperationOutcome"}]),
    )
    responses.add(
        responses.GET,
        f"{ISS}/Observation",
        match=[responses.matchers.query_param_matcher({"category": "vital-signs", "patient": "p1"})],
        json=search([obs(3, "vital-signs"), obs(4, "vital-signs")]),
    )
    j, spool = job(), MemorySpoolHandle("j")
    saves = []
    assert fetch_resources(j, spool, source_settings(), make_client(), lambda: saves.append(1), LOG)
    labs = next(o for o in j.outcomes if o.label == "Labs")
    vitals = next(o for o in j.outcomes if o.label == "Vital Signs")
    assert (labs.status, labs.count, labs.pages) == (OutcomeStatus.OK, 3, 2)
    assert (vitals.status, vitals.count) == (OutcomeStatus.OK, 1)  # o3 already spooled under Labs
    assert [r["id"] for r in spool.iter_type("Observation")] == ["o1", "o2", "o3", "o4"]
    assert spool.index().types["Patient"] == 1 and saves


@responses.activate
def test_one_types_failure_does_not_stop_the_others():
    responses.add(responses.GET, f"{ISS}/Patient/p1", json={"resourceType": "Patient", "id": "p1"})
    responses.add(
        responses.GET,
        f"{ISS}/Observation",
        status=500,
        json={"resourceType": "OperationOutcome", "issue": [{"diagnostics": "MRN 12345 unavailable"}]},
    )
    j = job()
    assert fetch_resources(j, MemorySpoolHandle("j"), source_settings(), make_client(), lambda: None, LOG)
    labs = next(o for o in j.outcomes if o.label == "Labs")
    assert labs.status is OutcomeStatus.ERROR and labs.error == "500 MRN 12345 unavailable"
    assert next(o for o in j.outcomes if o.label == "Demographics").status is OutcomeStatus.OK


@responses.activate
def test_error_text_never_reaches_the_log(caplog):
    responses.add(
        responses.GET,
        f"{ISS}/Patient/p1",
        status=500,
        json={"resourceType": "OperationOutcome", "issue": [{"diagnostics": "patient Jane Doe MRN 98765"}]},
    )
    responses.add(responses.GET, f"{ISS}/Observation", json=search([]))
    with caplog.at_level(logging.DEBUG):
        fetch_resources(job(), MemorySpoolHandle("j"), source_settings(), make_client(), lambda: None, LOG)
    assert "98765" not in caplog.text and "tok" not in caplog.text


@responses.activate
def test_a_401_stops_the_fetch():
    responses.add(responses.GET, f"{ISS}/Patient/p1", status=401)
    with pytest.raises(FetchAborted):
        fetch_resources(job(), MemorySpoolHandle("j"), source_settings(), make_client(), lambda: None, LOG)


@responses.activate
def test_an_expiring_token_stops_further_requests():
    responses.add(responses.GET, f"{ISS}/Patient/p1", json={"resourceType": "Patient", "id": "p1"})
    clock = Clock()
    client = make_client(clock, minutes=1)  # inside the 30 s margin after the first call
    j = job()
    clock.advance(seconds=45)
    assert not fetch_resources(j, MemorySpoolHandle("j"), source_settings(), client, lambda: None, LOG)
    pending = [o for o in j.outcomes if not o.status.withheld]
    assert all(o.status is OutcomeStatus.ERROR for o in pending)
    assert len(responses.calls) == 0


@responses.activate
def test_resume_refetches_an_unsettled_type_from_scratch():
    responses.add(responses.GET, f"{ISS}/Patient/p1", json={"resourceType": "Patient", "id": "p1"})
    responses.add(responses.GET, f"{ISS}/Observation", json=search([obs(1)]))
    j, spool = job(), MemorySpoolHandle("j")
    spool.append("Observation", obs(99))  # what a dead run left behind
    fetch_resources(j, spool, source_settings(), make_client(), lambda: None, LOG)
    assert [r["id"] for r in spool.iter_type("Observation")] == ["o1"]


@responses.activate
def test_a_page_that_is_not_a_search_bundle_fails_only_its_own_row():
    responses.add(responses.GET, f"{ISS}/Patient/p1", json={"resourceType": "Patient", "id": "p1"})
    responses.add(responses.GET, f"{ISS}/Observation", json=[])  # a 200 that is not an object
    j = job()
    assert fetch_resources(j, MemorySpoolHandle("j"), source_settings(), make_client(), lambda: None, LOG)
    labs = next(o for o in j.outcomes if o.label == "Labs")
    assert labs.status is OutcomeStatus.ERROR and labs.error == "unreadable response"
    assert next(o for o in j.outcomes if o.label == "Demographics").status is OutcomeStatus.OK


@pytest.mark.parametrize(
    "body",
    [
        {"resourceType": "Patient"},  # not a Bundle
        {"resourceType": "Bundle", "entry": ["text"]},  # an entry that is not an object
        {"resourceType": "Bundle", "entry": [{"resource": "text"}]},  # a resource that is not one
        {"resourceType": "Bundle", "entry": [{"resource": {"id": "o1"}}]},  # a resource without a type
    ],
)
@responses.activate
def test_malformed_search_pages_are_unreadable(body):
    responses.add(responses.GET, f"{ISS}/Patient/p1", json={"resourceType": "Patient", "id": "p1"})
    responses.add(responses.GET, f"{ISS}/Observation", json=body)
    j = job()
    fetch_resources(j, MemorySpoolHandle("j"), source_settings(), make_client(), lambda: None, LOG)
    assert next(o for o in j.outcomes if o.label == "Labs").error == "unreadable response"


@pytest.mark.parametrize(
    "next_url",
    [
        "https://elsewhere.example.org/fhir/Observation?page=2",  # another host
        "https://ehr.example.org:8443/fhir/Observation?page=2",  # another port
    ],
)
@responses.activate
def test_a_next_link_on_another_host_is_followed_with_the_token(next_url):
    """The EHR wrote the link: cross-host paging is supported, token and all."""
    responses.add(responses.GET, f"{ISS}/Patient/p1", json={"resourceType": "Patient", "id": "p1"})
    responses.add(responses.GET, f"{ISS}/Observation", json=search([obs(1)], next_url=next_url))
    responses.add(responses.GET, next_url, json=search([obs(2)]))
    j = job()
    fetch_resources(j, MemorySpoolHandle("j"), source_settings(), make_client(), lambda: None, LOG)
    assert next(o for o in j.outcomes if o.label == "Labs").count == 2
    assert responses.calls[-1].request.headers["Authorization"] == "Bearer tok"


@pytest.mark.parametrize(
    ("next_url", "reason"),
    [
        ("http://ehr.example.org/fhir/Observation?page=2", UrlRefusal.NOT_HTTPS),
        ("https://10.0.0.7/fhir/Observation?page=2", UrlRefusal.PRIVATE_ADDRESS),
        ("https://ehr.example.org:abc/fhir/Observation?page=2", UrlRefusal.UNPARSEABLE),
    ],
)
@responses.activate
def test_a_next_link_the_policy_refuses_ends_that_type_only(next_url, reason):
    responses.add(responses.GET, f"{ISS}/Patient/p1", json={"resourceType": "Patient", "id": "p1"})
    responses.add(responses.GET, f"{ISS}/Observation", json=search([obs(1)], next_url=next_url))
    j = job()
    fetch_resources(j, MemorySpoolHandle("j"), source_settings(), make_client(), lambda: None, LOG)
    labs = next(o for o in j.outcomes if o.label == "Labs")
    assert labs.status is OutcomeStatus.ERROR and labs.error.endswith(reason.value)
    assert next(o for o in j.outcomes if o.label == "Demographics").status is OutcomeStatus.OK
    assert all(c.request.url.startswith(ISS) for c in responses.calls)


@responses.activate
def test_a_next_link_elsewhere_on_the_same_origin_is_followed():
    """Lenient on the path: some servers page through another base path on the same host."""
    other_path = "https://ehr.example.org/paging/Observation?page=2"
    responses.add(responses.GET, f"{ISS}/Patient/p1", json={"resourceType": "Patient", "id": "p1"})
    responses.add(responses.GET, f"{ISS}/Observation", json=search([obs(1)], next_url=other_path))
    responses.add(responses.GET, other_path, json=search([obs(2)]))
    j = job()
    fetch_resources(j, MemorySpoolHandle("j"), source_settings(), make_client(), lambda: None, LOG)
    assert next(o for o in j.outcomes if o.label == "Labs").count == 2


@responses.activate
def test_resume_resets_every_row_of_the_type_so_none_reads_as_a_stale_ok():
    """Labs failed and Vital Signs was ok in an earlier run; on the resume the token runs out
    before the Observation type is even asked for (the Patient request uses the last of it), so
    Labs' first request raises. Vital Signs must not keep its old "ok, 2": every row of the type
    is reset before the refetch, not one by one as each is reached."""
    clock = Clock()
    j = job()
    for o in j.outcomes:
        if o.label == "Vital Signs":
            o.status, o.count = OutcomeStatus.OK, 2

    def last_of_the_token(request):
        clock.advance(hours=2)
        return 200, {}, '{"resourceType": "Patient", "id": "p1"}'

    responses.add_callback(responses.GET, f"{ISS}/Patient/p1", callback=last_of_the_token)
    fetch_resources(j, MemorySpoolHandle("j"), source_settings(), make_client(clock), lambda: None, LOG)
    labs = next(o for o in j.outcomes if o.label == "Labs")
    vitals = next(o for o in j.outcomes if o.label == "Vital Signs")
    assert labs.status is OutcomeStatus.ERROR
    assert vitals.status is not OutcomeStatus.OK and vitals.count == 0
    assert all(c.request.url.startswith(f"{ISS}/Patient") for c in responses.calls)  # nothing else was asked


def test_a_retry_after_longer_than_the_cap_fails_the_row_instead_of_sleeping(monkeypatch):
    from patient_access.core import http

    slept = []
    monkeypatch.setattr(http.Retry, "sleep_for_retry", lambda self, response=None: slept.append(1) or True)
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.add(responses.GET, f"{ISS}/Patient/p1", json={"resourceType": "Patient", "id": "p1"})
        rsps.add(responses.GET, f"{ISS}/Observation", status=429, headers={"Retry-After": "3600"})
        j = job()
        client = FhirClient(ISS, SecretStr("tok"), source_settings().fetch, None, Clock(), http.session())
        fetch_resources(j, MemorySpoolHandle("j"), source_settings(), client, lambda: None, LOG)
    labs = next(o for o in j.outcomes if o.label == "Labs")
    assert labs.status is OutcomeStatus.ERROR and slept == []


def test_the_retry_cap_refuses_long_waits_and_allows_short_ones(monkeypatch):
    import time as time_module

    from urllib3 import HTTPResponse

    from patient_access.core.http import MAX_RETRY_AFTER, CappedRetry, RetryAfterTooLong

    waits = []
    monkeypatch.setattr(time_module, "sleep", waits.append)
    retry = CappedRetry(total=3, respect_retry_after_header=True)
    with pytest.raises(RetryAfterTooLong):
        retry.sleep_for_retry(HTTPResponse(status=429, headers={"Retry-After": str(int(MAX_RETRY_AFTER) + 1)}))
    assert retry.sleep_for_retry(HTTPResponse(status=429, headers={"Retry-After": "5"}))
    assert waits == [5.0]


def test_outcomes_are_planned_with_their_status_not_patched_after():
    outcomes = plan_outcomes(source_settings(), ("patient/Observation.read",), ("patient/Observation.read",))
    statuses = {o.label: o.status for o in outcomes}
    assert statuses["Labs"] is OutcomeStatus.PENDING and statuses["Allergies"] is OutcomeStatus.NOT_REQUESTED


@responses.activate
def test_non_finite_numbers_from_the_ehr_become_null_and_are_logged(caplog):
    body = '{"resourceType":"Bundle","entry":[{"resource":{"resourceType":"Observation","id":"o1",'
    body += '"valueQuantity":{"value":NaN},"referenceRange":[{"high":{"value":Infinity}}]}}]}'
    responses.add(responses.GET, f"{ISS}/Patient/p1", json={"resourceType": "Patient", "id": "p1"})
    responses.add(responses.GET, f"{ISS}/Observation", body=body, content_type="application/fhir+json")
    spool = MemorySpoolHandle("j")
    with caplog.at_level(logging.WARNING):
        fetch_resources(job(), spool, source_settings(), make_client(), lambda: None, LOG)
    (o1,) = list(spool.iter_type("Observation"))
    assert o1["valueQuantity"]["value"] is None and o1["referenceRange"][0]["high"]["value"] is None
    assert "2 non-finite numbers replaced" in caplog.text
