"""run_job end to end over the in-memory plugs and a fake EHR: phases, resume, retries, the
delivery guard, and what a terminal job leaves behind."""

import json

import pytest
import responses

from patient_access.core.models import DeliveryStatus, JobStatus, OutcomeStatus, PendingUpload
from patient_access.core.pipeline import DeliveryRefused, Deps, run_job
from patient_access.formatters import render_bundle
from patient_access.formatters.metadata import pull_metadata
from patient_access.testing.memory import MemoryDirectory, MemoryJobStore, MemorySink, MemorySpool

from .helpers import ISS, SELECTION, Clock, job, settings, source_settings

KEY = "40001/7/20261104T120000Z/bundle.json"


@pytest.fixture
def ehr():
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.add(responses.GET, f"{ISS}/Patient/p1", json={"resourceType": "Patient", "id": "p1"})
        rsps.add(
            responses.GET,
            f"{ISS}/Observation",
            json={"resourceType": "Bundle", "entry": [{"resource": {"resourceType": "Observation", "id": "o1"}}]},
        )
        yield rsps


class World:
    def __init__(self, **deps_overrides):
        self.clock = Clock()
        self.jobs = MemoryJobStore(self.clock)
        self.spool = MemorySpool()
        self.sink = MemorySink()
        self.guarded = []
        self.settings = settings()
        values = {
            "jobs": self.jobs,
            "spool": self.spool,
            "directory": MemoryDirectory([SELECTION], lambda e: source_settings()),
            "sink": self.sink,
            "settings_provider": lambda: self.settings,
            "delivery_guard": self.guarded.append,
            "render": render_bundle,
            "clock": self.clock,
        } | deps_overrides
        self.deps = Deps(**values)
        self.job = self.jobs.create(job())

    def run(self):
        return run_job(self.job.id, self.deps)


def test_a_pull_runs_to_done_and_leaves_nothing_behind(ehr):
    w = World()
    done = w.run()
    assert done.status is JobStatus.DONE and done.failure is None
    assert done.delivery.status is DeliveryStatus.DONE and done.delivery.receipt.key == KEY
    assert done.credentials.ehr_access_token is None and done.credentials.spool_key is None
    assert done.spool_ref is None and w.spool.handles == {}
    assert done.delivery.pending_upload is None
    bundle = json.loads(w.sink.objects[KEY])
    meta = pull_metadata(bundle)
    assert {o.label: o.status for o in meta.outcomes}["Labs"] is OutcomeStatus.OK
    assert [g.id for g in w.guarded] == [done.id]
    assert w.jobs.get(done.id).status is JobStatus.DONE


def test_a_terminal_job_is_not_run_again(ehr):
    w = World()
    w.run()
    calls = len(ehr.calls)
    assert w.run().status is JobStatus.DONE and len(ehr.calls) == calls


def test_a_failed_delivery_retries_from_the_spool_without_the_ehr(ehr):
    w = World(sink=MemorySink(fail_times=1))
    first = w.run()
    assert first.status is JobStatus.FAILED and first.delivery.status is DeliveryStatus.FAILED
    calls = len(ehr.calls)
    second = w.run()
    assert second.status is JobStatus.DONE and len(ehr.calls) == calls
    assert second.delivery.attempts == 2


def test_delivery_gives_up_after_the_last_attempt(ehr):
    w = World(sink=MemorySink(fail_times=5), max_attempts=2)
    w.run()
    final = w.run()
    assert final.status is JobStatus.FAILED_TERMINAL and final.delivery.status is DeliveryStatus.FAILED_TERMINAL
    assert final.credentials.ehr_access_token is None and w.spool.handles == {}


def test_the_delivery_guard_can_refuse_and_nothing_is_written(ehr):
    def withdrawn(job):
        raise DeliveryRefused("You are no longer sharing records with this study.")

    w = World(delivery_guard=withdrawn)
    final = w.run()
    assert final.status is JobStatus.FAILED_TERMINAL
    assert final.failure == "You are no longer sharing records with this study."
    assert w.sink.objects == {}


def test_unusable_settings_leave_the_job_to_try_again(ehr):
    w = World()

    def broken():
        raise ValueError("no bucket")

    w.deps = Deps(**{**w.deps.__dict__, "settings_provider": broken})
    first = w.run()
    assert first.status is JobStatus.QUEUED and first.failure == "settings not usable: ValueError"
    w.deps = Deps(**{**w.deps.__dict__, "settings_provider": lambda: w.settings})
    assert w.run().status is JobStatus.DONE


def test_a_lost_spool_is_refetched_while_the_token_lives(ehr):
    w = World(sink=MemorySink(fail_times=1))
    w.run()  # fetched, delivery failed
    w.spool.handles.clear()  # the machine went away
    calls = len(ehr.calls)
    final = w.run()
    assert final.status is JobStatus.DONE and len(ehr.calls) > calls


def test_a_lost_spool_after_the_token_expired_fails_honestly(ehr):
    w = World(sink=MemorySink(fail_times=1))
    w.run()
    w.spool.handles.clear()
    w.clock.advance(hours=2)
    final = w.run()
    assert final.status is JobStatus.FAILED_TERMINAL and "connect again" in final.failure


def test_an_expired_token_before_the_fetch_fails_the_job(ehr):
    w = World()
    w.clock.advance(hours=2)
    assert w.run().status is JobStatus.FAILED_TERMINAL
    assert len(ehr.calls) == 0


def test_an_upload_a_dead_run_left_open_is_aborted_first(ehr):
    w = World()
    stale = PendingUpload(bucket="pulls", key="k", upload_id="u-1")
    j = w.jobs.get(w.job.id)
    j.status = JobStatus.FETCHED
    j.delivery.pending_upload = stale
    w.jobs.save(j)
    w.run()
    assert w.sink.aborted == [stale]


def test_a_401_during_the_fetch_is_terminal():
    w = World()
    with responses.RequestsMock() as rsps:
        rsps.add(responses.GET, f"{ISS}/Patient/p1", status=401)
        final = w.run()
    assert final.status is JobStatus.FAILED_TERMINAL and "connect again" in final.failure
