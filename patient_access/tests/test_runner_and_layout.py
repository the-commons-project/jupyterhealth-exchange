"""The runner's lease (claim, heartbeat, release, crashes) and the key layout."""

import threading
import time
from datetime import timedelta

import pytest

from patient_access.core.lease import Lease, LeaseLost
from patient_access.core.models import JobStatus
from patient_access.core.runner import LAPSE_MARGIN, MAX_CRASHES, JobRunner, _Heartbeat, sweep
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


def test_the_heartbeat_retries_a_failing_renewal_and_gives_up_a_margin_before_the_lease_lapses():
    """A worker that cannot renew must stop before the database frees the lease, never after: a
    new owner may share its spool directory. With a 1.2 s lease the renewals come at 0.4 and 0.8 s
    and the margin is at 1.0 s."""
    failures = []

    def unreachable(*args):
        failures.append(time.monotonic())
        raise ConnectionError("database down")

    period = timedelta(seconds=1.2)
    beat, lease = _beat(unreachable, period)
    started = time.monotonic()
    with beat:
        while not lease.cancelled and time.monotonic() < started + 3:
            time.sleep(0.005)
    cancelled_after = time.monotonic() - started
    assert lease.cancelled
    assert len(failures) >= 2  # retried, not given up on the first failure
    assert period.total_seconds() * (1 - LAPSE_MARGIN) - 0.05 <= cancelled_after < period.total_seconds()


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


def test_the_default_layout_is_a_prefix_and_the_module_appends_the_pulls_part():
    layout = TemplateLayout("{attrs.patient_id}/{attrs.fhir_source_id}")
    assert layout.render(job()) == "40001/7/20261104T120000Z-job-1/bundle.json"


def test_two_pulls_never_share_a_key_whatever_the_prefix():
    """The appended part carries the start time and the job id, so even an empty prefix or one
    naming only the patient keeps every pull."""
    a, b = job(job_id="aaaaaaaa-1"), job(job_id="bbbbbbbb-2")
    for template in ("", "{attrs.patient_id}", "static"):
        assert TemplateLayout(template).render(a) != TemplateLayout(template).render(b)
    assert TemplateLayout("").render(a) == "20261104T120000Z-aaaaaaaa/bundle.json"


def test_values_cannot_escape_their_segment():
    j = job()
    j.source = j.source.model_copy(update={"brand_name": "../../etc/passwd"})
    key = TemplateLayout("{source.brand_name}").render(j)
    assert key.startswith(".._.._etc_passwd/") and len(key.split("/")) == 3
    j.source = j.source.model_copy(update={"brand_name": ".."})
    assert TemplateLayout("{source.brand_name}").render(j).startswith("_/")


@pytest.mark.parametrize(
    ("template", "message"),
    [
        ("{job.id}", "appended by the module"),
        ("{attrs.patient_id}/{created_at:%Y}", "appended by the module"),
        ("{created_at}", "appended by the module"),
        ("{secrets.key}", "unknown"),
        ("{job.status}", "unknown"),
        ("a//b", "empty segment"),
        ("{attrs.patient_id", "not a valid template"),
        ("{job.__init__.__globals__}", "private"),
        ("{job._secret}", "private"),
        ("{attrs[patient_id]}", "index"),
        ("{source.id!r}", "conversion"),
        ("{source.id:>40}", "format"),
    ],
)
def test_bad_layouts_are_refused_with_a_reason(template, message):
    with pytest.raises(LayoutError, match=message):
        TemplateLayout(template)


def test_the_traversal_that_reached_the_environment_is_refused():
    """The review's demonstration: a template walking from the job to os.environ."""
    template = "{job.__init__.__globals__[sys].modules[os].environ[PATIENT_ACCESS_ENCRYPTION_KEYS]}"
    with pytest.raises(LayoutError):
        TemplateLayout(template)


def test_a_field_the_job_lacks_fails_at_render():
    with pytest.raises(LayoutError, match="not available"):
        TemplateLayout("{attrs.study}").render(job())


def test_two_workers_on_one_job_at_once_run_it_exactly_once():
    """The lease is the only shared structure: whichever claims first runs, the other is told no."""
    jobs = MemoryJobStore(Clock())
    j = jobs.create(job())
    gate = threading.Event()
    ran = []

    def slow(lease):
        ran.append(lease.worker)
        gate.wait(2)
        return jobs.get(lease.job_id)

    results = {}

    def run_as(name):
        results[name] = JobRunner(jobs, slow, lease=timedelta(seconds=60), worker=lambda: name).run(j.id)

    threads = [threading.Thread(target=run_as, args=(name,)) for name in ("a", "b")]
    for t in threads:
        t.start()
    time.sleep(0.1)
    gate.set()
    for t in threads:
        t.join(timeout=5)
    assert sorted(results.values()) == [False, True] and len(ran) == 1


def test_runner_lines_carry_the_job_id_for_structured_handlers(caplog):
    """Every line the runner writes goes through JobLog: the job id in `extra` and the message."""
    import logging

    jobs = MemoryJobStore(Clock())
    j = jobs.create(job())

    def explode(lease):
        raise RuntimeError("bug")

    with caplog.at_level(logging.INFO, logger="patient_access"):
        JobRunner(jobs, explode, lease=timedelta(seconds=60), worker=lambda: "w").run(j.id)
    lines = [r for r in caplog.records if r.name.startswith("patient_access")]
    assert lines
    assert all(getattr(r, "job_id", None) == j.id for r in lines)
    assert all(r.getMessage().startswith(f"job {j.id} [lease]") for r in lines)
