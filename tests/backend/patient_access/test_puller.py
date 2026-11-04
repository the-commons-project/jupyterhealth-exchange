"""Phase 4's resource walk: paging, per-type isolation, dedupe, expiry, the 401 stop."""

import logging
from datetime import timedelta

import pytest
import responses
from pydantic import SecretStr

from patient_access.core.client import FhirClient
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
