"""run_job end to end over the in-memory plugs and a fake EHR: phases, resume, retries, the
delivery guard, failure kinds, the lease fence, and what a terminal job leaves behind."""

import json
from datetime import timedelta

import pytest
import responses

from patient_access.core.lease import Lease, LeaseLost
from patient_access.core.models import DeliveryStatus, JobStatus, OutcomeStatus, PendingUpload
from patient_access.core.pipeline import DeliveryRefused, Deps, run_job
from patient_access.core.vocab import FailureKind
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
            "delivery_guard": self.guard,
            "render": render_bundle,
            "clock": self.clock,
        } | deps_overrides
        self.deps = Deps(**values)
        self.job = self.jobs.create(job())

    def guard(self, j):
        self.guarded.append(j)
        return j.participant.attrs

    def run(self, worker="test"):
        return run_job(self.jobs.lease_for(self.job.id, worker), self.deps)

    def stored(self):
        return self.jobs.get(self.job.id)


def test_a_pull_runs_to_done_and_leaves_nothing_behind(ehr):
    w = World()
    done = w.run()
    assert done.status is JobStatus.DONE and done.failure is None and done.failure_kind is None
    assert done.delivery.status is DeliveryStatus.DONE and done.delivery.receipt.key == KEY
    assert done.credentials.ehr_access_token is None and done.credentials.spool_key is None
    assert done.spool_ref is None and w.spool.handles == {}
    assert done.delivery.pending_uploads == []
    bundle = json.loads(w.sink.objects[KEY])
    meta = pull_metadata(bundle)
    assert {o.label: o.status for o in meta.outcomes}["Labs"] is OutcomeStatus.OK
    assert [g.id for g in w.guarded] == [done.id]
    assert w.stored().status is JobStatus.DONE


def test_a_terminal_job_is_not_run_again(ehr):
    w = World()
    w.run()
    calls = len(ehr.calls)
    assert not w.jobs.claim(w.job.id, "test", timedelta(minutes=1))  # never claimed again
    assert run_job(Lease(job_id=w.job.id, worker="test"), w.deps).status is JobStatus.DONE
    assert len(ehr.calls) == calls  # and a run handed one anyway does nothing


def test_a_failed_delivery_retries_from_the_spool_without_the_ehr(ehr):
    w = World(sink=MemorySink(fail_times=1))
    first = w.run()
    assert first.status is JobStatus.FAILED and first.delivery.status is DeliveryStatus.FAILED
    assert first.failure_kind is FailureKind.RETRY_LATER
    calls = len(ehr.calls)
    second = w.run()
    assert second.status is JobStatus.DONE and len(ehr.calls) == calls
    assert second.delivery.attempts == 2 and second.failure_kind is None


def test_delivery_gives_up_after_the_last_attempt(ehr):
    w = World(sink=MemorySink(fail_times=5), max_attempts=2)
    w.run()
    final = w.run()
    assert final.status is JobStatus.FAILED_TERMINAL and final.delivery.status is DeliveryStatus.FAILED_TERMINAL
    assert final.failure_kind is FailureKind.CONTACT_STUDY
    assert final.credentials.ehr_access_token is None and w.spool.handles == {}


def test_the_delivery_guard_can_refuse_and_nothing_is_written(ehr):
    def withdrawn(job):
        raise DeliveryRefused("You are no longer sharing records with this study.")

    w = World(delivery_guard=withdrawn)
    final = w.run()
    assert final.status is JobStatus.FAILED_TERMINAL
    assert final.failure == "You are no longer sharing records with this study."
    assert final.failure_kind is FailureKind.CONSENT_WITHDRAWN
    assert final.delivery.status is DeliveryStatus.FAILED_TERMINAL  # the delivery says so too
    assert w.sink.objects == {}


def test_a_guard_that_raises_anything_else_counts_as_a_failed_attempt(ehr):
    calls = []

    def flaky(job):
        calls.append(job.id)
        if len(calls) == 1:
            raise ConnectionError("database briefly unreachable")
        return job.participant.attrs

    w = World(delivery_guard=flaky)
    first = w.run()
    assert first.status is JobStatus.FAILED and first.delivery.attempts == 1
    assert first.failure_kind is FailureKind.RETRY_LATER
    assert w.run().status is JobStatus.DONE


def test_the_bundle_records_the_attributes_the_guard_returns(ehr):
    """The guard re-reads the participant at delivery (JHE: the studies still consented to); the
    bundle records that, not what was true when the pull started."""
    w = World(delivery_guard=lambda j: j.participant.attrs.model_copy(update={"fhir_source_id": 99}))
    done = w.run()
    meta = pull_metadata(json.loads(next(iter(w.sink.objects.values()))))
    assert done.participant.attrs.fhir_source_id == 99
    assert meta.participant_attrs["fhir_source_id"] == 99


def test_unusable_settings_leave_the_job_to_try_again(ehr):
    w = World()

    def broken():
        raise ValueError("no bucket")

    w.deps = Deps(**{**w.deps.__dict__, "settings_provider": broken})
    first = w.run()
    assert first.status is JobStatus.QUEUED and first.failure == "settings not usable: ValueError"
    assert first.failure_kind is FailureKind.RETRY_LATER
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
    assert final.failure_kind is FailureKind.RECONNECT


def test_an_expired_token_before_the_fetch_fails_the_job(ehr):
    w = World()
    w.clock.advance(hours=2)
    final = w.run()
    assert final.status is JobStatus.FAILED_TERMINAL and final.failure_kind is FailureKind.RECONNECT
    assert len(ehr.calls) == 0


def test_a_token_that_expires_mid_pull_fails_the_job_and_delivers_nothing():
    """All or nothing: part of a record is never delivered as if it were the whole."""
    w = World()

    def patient_then_expire(request):
        w.clock.advance(hours=2)  # the token runs out while Demographics is being fetched
        return 200, {}, json.dumps({"resourceType": "Patient", "id": "p1"})

    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.add_callback(responses.GET, f"{ISS}/Patient/p1", callback=patient_then_expire)
        final = w.run()
    assert final.status is JobStatus.FAILED_TERMINAL and final.failure_kind is FailureKind.RECONNECT
    assert final.documents.status is OutcomeStatus.ERROR and "authorization ended" in final.documents.error
    assert w.sink.objects == {} and w.spool.handles == {}


def test_an_upload_a_dead_run_left_open_is_aborted_first(ehr):
    w = World()
    stale = [PendingUpload(bucket="pulls", key="k", upload_id=f"u-{i}") for i in (1, 2)]
    lease = w.jobs.lease_for(w.job.id, "dead")
    j = w.jobs.get(w.job.id)
    j.status = JobStatus.FETCHED
    j.delivery.pending_uploads = stale
    w.jobs.save(j, lease)
    w.jobs.release(w.job.id, "dead")
    w.run()
    assert w.sink.aborted == stale  # every one, not only the last


def test_a_401_during_the_fetch_is_terminal():
    w = World()
    with responses.RequestsMock() as rsps:
        rsps.add(responses.GET, f"{ISS}/Patient/p1", status=401)
        final = w.run()
    assert final.status is JobStatus.FAILED_TERMINAL and "connect again" in final.failure
    assert final.failure_kind is FailureKind.RECONNECT


def test_a_worker_that_lost_its_lease_writes_nothing_more():
    """The web thread stalls past its lease; cron takes the job. The stale worker's next save is
    refused, so it can never overwrite what the new owner writes."""
    w = World()

    def stall_then_lose(request):
        w.clock.advance(minutes=11)  # past the 10-minute test lease
        assert w.jobs.claim(w.job.id, "cron", timedelta(minutes=10))
        return 200, {}, json.dumps({"resourceType": "Patient", "id": "p1"})

    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.add_callback(responses.GET, f"{ISS}/Patient/p1", callback=stall_then_lose)
        rsps.add(responses.GET, f"{ISS}/Observation", json={"resourceType": "Bundle"})
        with pytest.raises(LeaseLost):
            w.run(worker="web")
    assert w.stored().status is JobStatus.FETCHING  # the last write the web worker was allowed
    assert w.sink.objects == {}


def test_a_cancelled_lease_stops_before_the_next_request(ehr):
    w = World()
    lease = w.jobs.lease_for(w.job.id)
    lease.cancel()
    with pytest.raises(LeaseLost):
        run_job(lease, w.deps)
    assert len(ehr.calls) == 0
