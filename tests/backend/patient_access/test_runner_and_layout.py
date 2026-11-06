"""The runner's lease (claim, heartbeat, release, crashes) and the key layout."""

import threading
import time
from datetime import timedelta

import pytest

from patient_access.core.lease import Lease, LeaseLost
from patient_access.core.models import JobStatus
from patient_access.core.runner import MAX_CRASHES, JobRunner, _Heartbeat, sweep
from patient_access.core.vocab import FailureKind
from patient_access.destinations.layout import LayoutError, TemplateLayout
from patient_access.dispatch.thread import ThreadDispatcher
from patient_access.testing.memory import MemoryAttemptStore, MemoryJobStore, MemorySpool

from .helpers import Clock, job

# --- the runner ----------------------------------------------------------------------------


def test_a_leased_job_cannot_be_claimed_by_another_worker():
    jobs = MemoryJobStore(Clock())
    j = jobs.create(job())
    assert jobs.claim(j.id, "a", timedelta(seconds=60))
    assert not jobs.claim(j.id, "b", timedelta(seconds=60))
    assert jobs.claim_next("b", timedelta(seconds=60), timedelta(0)) is None
    jobs.release(j.id, "a")
    assert jobs.claim_next("b", timedelta(seconds=60), timedelta(0)) == j.id


def test_an_expired_lease_is_free_again():
    clock = Clock()
    jobs = MemoryJobStore(clock)
    j = jobs.create(job())
    jobs.claim(j.id, "dead", timedelta(seconds=60))
    clock.advance(seconds=61)
    assert jobs.claim_next("sweep", timedelta(seconds=60), timedelta(0)) == j.id


def test_claim_next_skips_the_jobs_it_is_told_to():
    jobs = MemoryJobStore(Clock())
    first, second = jobs.create(job("j1")), jobs.create(job("j2"))
    assert jobs.claim_next("w", timedelta(seconds=60), timedelta(0), frozenset({first.id})) == second.id


def test_a_stale_save_cannot_overwrite_the_new_owner():
    clock = Clock()
    jobs = MemoryJobStore(clock)
    j = jobs.create(job())
    stale = jobs.lease_for(j.id, "web", timedelta(seconds=60))
    clock.advance(seconds=61)
    fresh = jobs.lease_for(j.id, "cron", timedelta(seconds=60))
    done = jobs.get(j.id)
    done.status = JobStatus.DONE
    jobs.save(done, fresh)
    late = jobs.get(j.id)
    late.status = JobStatus.FAILED_TERMINAL
    with pytest.raises(LeaseLost):
        jobs.save(late, stale)
    assert jobs.get(j.id).status is JobStatus.DONE


def test_run_releases_the_lease_and_counts_a_crash_when_the_job_raises():
    jobs = MemoryJobStore(Clock())
    j = jobs.create(job())

    def explode(lease):
        raise RuntimeError("bug")

    runner = JobRunner(jobs, explode, lease=timedelta(seconds=60), worker=lambda: "w")
    assert runner.run(j.id)
    assert jobs.crashes[j.id] == 1
    assert jobs.claim(j.id, "other", timedelta(seconds=60))


def test_a_job_that_keeps_crashing_is_ended_with_its_credentials_cleared():
    jobs = MemoryJobStore(Clock())
    j = jobs.create(job())

    def explode(lease):
        raise ValueError("the row no longer validates")

    runner = JobRunner(jobs, explode, worker=lambda: "w")
    for _ in range(MAX_CRASHES):
        runner.run(j.id)
    ended = jobs.get(j.id)
    assert ended.status is JobStatus.FAILED_TERMINAL and ended.failure_kind is FailureKind.CONTACT_STUDY
    assert ended.credentials.ehr_access_token is None
    assert not runner.run(j.id)  # terminal: never claimed again


def test_one_crashing_job_never_holds_up_the_others():
    """The oldest job raises every time; the sweep still reaches the job behind it, and after
    MAX_CRASHES sweeps the broken one is ended."""
    jobs = MemoryJobStore(Clock())
    poison, victim = jobs.create(job("j1")), jobs.create(job("j2"))
    ran = []

    def run(lease):
        ran.append(lease.job_id)
        if lease.job_id == poison.id:
            raise KeyError("blob")
        j = jobs.get(lease.job_id)
        j.status = JobStatus.DONE
        jobs.save(j, lease)
        return j

    runner = JobRunner(jobs, run)
    assert runner.drain() == 2  # each once
    assert jobs.get(victim.id).status is JobStatus.DONE
    for _ in range(MAX_CRASHES - 1):
        runner.drain()
    assert jobs.get(poison.id).status is JobStatus.FAILED_TERMINAL
    assert runner.drain() == 0 and ran.count(poison.id) == MAX_CRASHES


def test_the_heartbeat_renews_the_lease_while_the_job_runs():
    jobs = MemoryJobStore(Clock())
    j = jobs.create(job())
    renewed = threading.Event()
    original = jobs.renew

    def renew(job_id, worker, lease):
        renewed.set()
        return original(job_id, worker, lease)

    jobs.renew = renew

    def slow(lease):
        assert renewed.wait(timeout=5)
        return jobs.get(lease.job_id)

    runner = JobRunner(jobs, slow, lease=timedelta(seconds=0.3), worker=lambda: "w")
    assert runner.run(j.id) and renewed.is_set()


def _beat(renew, period):
    """A heartbeat over a store whose renew is `renew`, on a fresh lease."""

    class Store:
        pass

    store = Store()
    store.renew = renew
    lease = Lease(job_id="j1", worker="w")
    return _Heartbeat(store, lease, period, lambda: None), lease


def test_the_heartbeat_cancels_at_once_when_the_row_has_a_new_owner():
    beat, lease = _beat(lambda *a: False, timedelta(seconds=0.15))
    with beat:
        deadline = time.monotonic() + 2
        while not lease.cancelled and time.monotonic() < deadline:
            time.sleep(0.01)
    assert lease.cancelled


def test_the_heartbeat_retries_a_failing_renewal_until_the_lease_would_lapse():
    failures = []

    def unreachable(*args):
        failures.append(time.monotonic())
        raise ConnectionError("database down")

    beat, lease = _beat(unreachable, timedelta(seconds=0.3))
    started = time.monotonic()
    with beat:
        while not lease.cancelled and time.monotonic() < started + 3:
            time.sleep(0.01)
    assert lease.cancelled
    assert len(failures) >= 2  # retried, not given up on the first failure
    assert time.monotonic() - started >= 0.3  # and only once the lease would really have lapsed


def test_drain_runs_every_runnable_job_once():
    jobs = MemoryJobStore(Clock())
    ids = [jobs.create(job(f"j{i}")).id for i in range(3)]
    ran = []

    def finish(lease):
        ran.append(lease.job_id)
        j = jobs.get(lease.job_id)
        j.status = JobStatus.DONE
        jobs.save(j, lease)
        return j

    assert JobRunner(jobs, finish).drain() == 3 and sorted(ran) == sorted(ids)


def test_drain_visits_a_job_that_stays_runnable_once():
    jobs = MemoryJobStore(Clock())
    jobs.create(job())
    assert JobRunner(jobs, lambda lease: jobs.get(lease.job_id)).drain() == 1


def test_release_all_frees_what_the_process_holds():
    jobs = MemoryJobStore(Clock())
    j = jobs.create(job())
    started, finish = threading.Event(), threading.Event()

    def blocking(lease):
        started.set()
        finish.wait(timeout=5)
        return jobs.get(lease.job_id)

    runner = JobRunner(jobs, blocking, worker=lambda: "web")
    ThreadDispatcher(runner.run).dispatch(j.id)
    assert started.wait(timeout=5)
    runner.release_all()
    assert jobs.claim(j.id, "sweep", timedelta(seconds=60))
    finish.set()


def test_sweep_deletes_unreferenced_spools_and_old_attempts():
    jobs, spool = MemoryJobStore(Clock()), MemorySpool()
    live = jobs.create(job())
    live.spool_ref = "live"
    jobs.save(live, jobs.lease_for(live.id))
    spool.open("live", b"k" * 32)
    spool.open("orphan", b"k" * 32)
    gone, swept = sweep(jobs, spool, MemoryAttemptStore())
    assert gone == ["orphan"] and swept == 0


# --- the layout ----------------------------------------------------------------------------


def test_the_default_layout_renders_attrs_and_the_pull_time():
    layout = TemplateLayout("{attrs.patient_id}/{attrs.fhir_source_id}/{created_at:%Y%m%dT%H%M%SZ}/bundle.json")
    assert layout.render(job()) == "40001/7/20261104T120000Z/bundle.json"


def test_values_cannot_escape_their_segment():
    j = job()
    j.source = j.source.model_copy(update={"brand_name": "../../etc/passwd"})
    key = TemplateLayout("{source.brand_name}/{job.id}.json").render(j)
    assert key == ".._.._etc_passwd/job-1.json" and len(key.split("/")) == 2
    j.source = j.source.model_copy(update={"brand_name": ".."})
    assert TemplateLayout("{source.brand_name}/{job.id}.json").render(j) == "_/job-1.json"


@pytest.mark.parametrize(
    ("template", "message"),
    [
        ("{attrs.patient_id}/bundle.json", "per-pull"),
        ("{secrets.key}/{job.id}", "unknown"),
        ("a//{job.id}", "empty segment"),
        ("", "empty"),
        ("{job.id", "not a valid template"),
    ],
)
def test_bad_layouts_are_refused_with_a_reason(template, message):
    with pytest.raises(LayoutError, match=message):
        TemplateLayout(template)


def test_a_field_the_job_lacks_fails_at_render():
    with pytest.raises(LayoutError, match="needs"):
        TemplateLayout("{attrs.study}/{job.id}").render(job())
