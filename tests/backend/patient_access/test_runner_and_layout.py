"""The runner's lease (claim, heartbeat, release) and the key layout."""

import threading
from datetime import timedelta

import pytest

from patient_access.core.models import JobStatus
from patient_access.core.runner import JobRunner, sweep
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


def test_run_releases_the_lease_even_when_the_job_raises():
    jobs = MemoryJobStore(Clock())
    j = jobs.create(job())

    def explode(job_id):
        raise RuntimeError("bug")

    runner = JobRunner(jobs, explode, lease=timedelta(seconds=60), worker=lambda: "w")
    assert runner.run(j.id)
    assert jobs.claim(j.id, "other", timedelta(seconds=60))


def test_the_heartbeat_renews_the_lease_while_the_job_runs():
    jobs = MemoryJobStore(Clock())
    j = jobs.create(job())
    renewed = threading.Event()
    original = jobs.renew

    def renew(job_id, worker, lease):
        renewed.set()
        return original(job_id, worker, lease)

    jobs.renew = renew

    def slow(job_id):
        assert renewed.wait(timeout=5)
        return jobs.get(job_id)

    runner = JobRunner(jobs, slow, lease=timedelta(seconds=0.3), worker=lambda: "w")
    assert runner.run(j.id) and renewed.is_set()


def test_drain_runs_every_runnable_job_once():
    jobs = MemoryJobStore(Clock())
    ids = [jobs.create(job(f"j{i}")).id for i in range(3)]
    ran = []

    def finish(job_id):
        ran.append(job_id)
        j = jobs.get(job_id)
        j.status = JobStatus.DONE
        jobs.save(j)
        return j

    assert JobRunner(jobs, finish).drain() == 3 and sorted(ran) == sorted(ids)


def test_drain_stops_on_a_job_that_stays_runnable():
    jobs = MemoryJobStore(Clock())
    jobs.create(job())
    assert JobRunner(jobs, jobs.get).drain() == 2  # ran once, came round again, stopped


def test_release_all_frees_what_the_process_holds():
    jobs = MemoryJobStore(Clock())
    j = jobs.create(job())
    started, finish = threading.Event(), threading.Event()

    def blocking(job_id):
        started.set()
        finish.wait(timeout=5)
        return jobs.get(job_id)

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
    jobs.save(live)
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
