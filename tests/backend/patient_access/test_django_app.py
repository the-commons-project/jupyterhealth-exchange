"""The Django app: the ORM stores (encryption at rest, single use, leases), the config, the
wiring, and the cron command."""

import io
import logging
from datetime import timedelta

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone
from pydantic import ValidationError

from patient_access.core.lease import Lease, LeaseLost
from patient_access.core.models import JobStatus
from patient_access.core.vocab import FailureKind
from patient_access.django_app.conf import ConfigError, parse
from patient_access.django_app.crypto import Crypto
from patient_access.django_app.models import AuthAttemptRecord, JobRecord
from patient_access.django_app.stores import OrmAuthAttemptStore, OrmJobStore
from patient_access.django_app.wiring import NotConfigured, build_module, get_module

from .helpers import TEST_KEY, HostAttrs, LaunchAttrs, attempt, job, module_config, participant

pytestmark = pytest.mark.django_db


@pytest.fixture
def crypto():
    return Crypto(parse(module_config()).encryption_keys)


# --- attempts ------------------------------------------------------------------------------


def test_an_attempt_round_trips_with_the_verifier_encrypted(crypto):
    store = OrmAuthAttemptStore(LaunchAttrs, crypto)
    a = attempt(expires_at=timezone.now() + timedelta(minutes=10))
    store.create(a)
    row = AuthAttemptRecord.objects.get(state=a.state)
    assert "verifier" not in row.code_verifier_enc and "verifier" not in str(row.body)
    back = store.consume(a.state)
    assert back == a and back.participant.attrs == LaunchAttrs(patient_id=40001, data_source_id=3)


def test_an_attempt_is_single_use_and_expires(crypto):
    store = OrmAuthAttemptStore(LaunchAttrs, crypto)
    store.create(attempt(state="s1", expires_at=timezone.now() + timedelta(minutes=10)))
    store.consume("s1")
    with pytest.raises(KeyError):
        store.consume("s1")
    store.create(attempt(state="s2", expires_at=timezone.now() - timedelta(seconds=1)))
    with pytest.raises(KeyError):
        store.consume("s2")
    store.create(attempt(state="s3", expires_at=timezone.now() - timedelta(days=2)))
    assert store.sweep(timedelta(days=1)) == 1


# --- jobs ----------------------------------------------------------------------------------


def leased(store, job_id, worker="a"):
    assert store.claim(job_id, worker, timedelta(seconds=60))
    return Lease(job_id=job_id, worker=worker)


def test_a_job_round_trips_with_its_credentials_encrypted(crypto):
    store = OrmJobStore(HostAttrs, crypto)
    created = store.create(job(job_id=""))
    row = JobRecord.objects.get(id=created.id)
    assert "ehr-token" not in row.credentials_enc and "ehr-token" not in str(row.body)
    assert row.status == "queued" and row.participant_ref == "40001"
    back = store.get(created.id)
    assert back.credentials.ehr_access_token.get_secret_value() == "ehr-token"
    assert back.participant.attrs == created.participant.attrs and back.outcomes == created.outcomes
    back.status, back.spool_ref = JobStatus.FETCHING, "spool-1"
    back.credentials.ehr_access_token = None
    store.save(back, leased(store, created.id))
    again = store.get(created.id)
    assert again.status is JobStatus.FETCHING and again.credentials.ehr_access_token is None
    assert store.live_spool_refs() == frozenset({"spool-1"})
    with pytest.raises(KeyError):
        store.get("missing")


def test_leases_claim_renew_and_release(crypto):
    store = OrmJobStore(HostAttrs, crypto)
    j = store.create(job(job_id=""))
    lease = timedelta(seconds=60)
    assert store.claim(j.id, "a", lease)
    assert not store.claim(j.id, "b", lease)
    assert store.claim_next("b", lease, timedelta(0)) is None
    assert store.renew(j.id, "a", lease) and not store.renew(j.id, "b", lease)
    store.release(j.id, "a")
    assert store.claim_next("b", lease, timedelta(0), frozenset({j.id})) is None  # excluded
    assert store.claim_next("b", lease, timedelta(0)) == j.id
    JobRecord.objects.filter(id=j.id).update(lease_expires_at=timezone.now() - timedelta(seconds=1))
    assert store.claim_next("c", lease, timedelta(0)) == j.id  # a dead worker's lease lapsed


def test_save_never_touches_the_lease(crypto):
    store = OrmJobStore(HostAttrs, crypto)
    j = store.create(job(job_id=""))
    store.save(store.get(j.id), leased(store, j.id))
    assert JobRecord.objects.get(id=j.id).lease_owner == "a"


def test_the_web_thread_and_the_sweep_on_one_row(crypto):
    """The web thread's lease lapses (it stalled); the sweep takes the row. From then on only
    the sweep's writes land: the stale thread can never put its terminal status over them."""
    store = OrmJobStore(HostAttrs, crypto)
    j = store.create(job(job_id=""))
    web = leased(store, j.id, "web")
    JobRecord.objects.filter(id=j.id).update(lease_expires_at=timezone.now() - timedelta(seconds=1))
    assert store.claim_next("cron", timedelta(seconds=60), timedelta(0)) == j.id
    cron = Lease(job_id=j.id, worker="cron")
    done = store.get(j.id)
    done.status = JobStatus.DONE
    store.save(done, cron)
    stale = store.get(j.id)
    stale.status = JobStatus.FAILED_TERMINAL
    with pytest.raises(LeaseLost):
        store.save(stale, web)
    assert store.get(j.id).status is JobStatus.DONE
    assert not store.renew(j.id, "web", timedelta(seconds=60))  # its heartbeat sees it too


def test_terminal_jobs_are_never_claimed_and_failed_ones_wait(crypto):
    store = OrmJobStore(HostAttrs, crypto)
    done = store.create(job(job_id=""))
    done.status = JobStatus.DONE
    store.save(done, leased(store, done.id))
    assert not store.claim(done.id, "b", timedelta(seconds=60))
    failed = store.create(job(job_id=""))
    failed.status = JobStatus.FAILED
    store.save(failed, leased(store, failed.id))
    store.release(failed.id, "a")
    assert store.claim_next("a", timedelta(seconds=60), timedelta(seconds=30)) is None
    JobRecord.objects.filter(id=failed.id).update(updated_at=timezone.now() - timedelta(minutes=1))
    assert store.claim_next("a", timedelta(seconds=60), timedelta(seconds=30)) == failed.id


def test_a_crashing_job_is_ended_even_when_its_body_no_longer_loads(crypto):
    store = OrmJobStore(HostAttrs, crypto)
    j = store.create(job(job_id=""))
    JobRecord.objects.filter(id=j.id).update(body={"id": j.id, "not": "a job any more"})
    with pytest.raises(ValidationError):
        store.get(j.id)
    lease = leased(store, j.id)
    assert [store.record_crash(lease) for _ in range(2)] == [1, 2]
    store.fail_crashed(lease, "stopped after 3 runs that failed unexpectedly")
    row = JobRecord.objects.get(id=j.id)
    assert row.status == JobStatus.FAILED_TERMINAL.value and row.credentials_enc == "" and row.finished_at
    assert row.body["failure_kind"] == FailureKind.CONTACT_STUDY.value and row.body["not"] == "a job any more"
    with pytest.raises(LeaseLost):
        store.record_crash(Lease(job_id=j.id, worker="someone else"))
    # Nothing of the job survives on that row, so the participant's list leaves it out, not 500s.
    assert [x.id for x in store.for_participant(participant().ref)] == []


def test_a_crash_capped_job_whose_progress_no_longer_loads_still_reads_as_a_failure(crypto):
    """The common breakage: participant and source are fine, a progress record is not (a schema
    change, an odd value). The patient's list then shows "delivery failed", not nothing."""
    store = OrmJobStore(HostAttrs, crypto)
    j = store.create(job(job_id=""))
    body = dict(JobRecord.objects.get(id=j.id).body)
    body["outcomes"] = [{"label": "Labs", "status": "no such status"}]
    JobRecord.objects.filter(id=j.id).update(body=body)
    with pytest.raises(ValidationError):
        store.get(j.id)
    store.fail_crashed(leased(store, j.id), "stopped after 3 runs that failed unexpectedly")
    ended = store.get(j.id)
    assert ended.status is JobStatus.FAILED_TERMINAL and ended.failure_kind is FailureKind.CONTACT_STUDY
    assert ended.outcomes == [] and ended.finished_at is not None
    assert ended.source.brand_name == j.source.brand_name and ended.participant.ref == j.participant.ref
    assert [x.id for x in store.for_participant(j.participant.ref)] == [j.id]


def test_a_participants_jobs_narrow_by_attribute_before_the_limit(crypto):
    store = OrmJobStore(HostAttrs, crypto)
    mine = store.create(job(job_id=""))
    for _ in range(3):
        store.create(job(job_id="", participant=participant(data_source_id=9)))
    found = store.for_participant("40001", {"data_source_id": 3}, limit=1)
    assert [f.id for f in found] == [mine.id]


# --- config and wiring ---------------------------------------------------------------------


def test_the_config_names_whats_wrong():
    with pytest.raises(ConfigError, match="LEASE_SECONDS"):
        parse(module_config(LEASE_SECONDS=1))
    with pytest.raises(ConfigError, match="UNKNOWN"):
        parse(module_config(UNKNOWN=1))
    with pytest.raises(ConfigError, match="ENCRYPTION_KEYS"):
        Crypto(parse(module_config(ENCRYPTION_KEYS=["not a key"])).encryption_keys)


def test_host_classes_are_checked():
    with pytest.raises(ConfigError, match="not a pydantic model"):
        build_module(parse(module_config(JOB_ATTRS="backend.patient_access.helpers.host_guard")))
    with pytest.raises(ConfigError, match="cannot be imported"):
        build_module(parse(module_config(DIRECTORY="nowhere.Directory")))


def test_the_module_is_off_without_keys_and_on_with_them():
    with override_settings(PATIENT_ACCESS={"ENCRYPTION_KEYS": []}):
        with pytest.raises(NotConfigured):
            get_module()
        call_command("patient_access_run_jobs", stdout=io.StringIO())  # nothing to do, no error
    with override_settings(PATIENT_ACCESS=module_config()):
        module = get_module()
        assert module.job_attrs is HostAttrs and module.launch_attrs is LaunchAttrs
    with pytest.raises(ImproperlyConfigured):
        with override_settings(PATIENT_ACCESS=module_config(DISPATCHER="carrier pigeon")):
            pass


def test_the_cron_command_runs_queued_jobs(tmp_path):
    with override_settings(PATIENT_ACCESS=module_config(SPOOL_DIR=str(tmp_path))):
        module = get_module()
        j = module.jobs.create(job(job_id="", token_expires_at=timezone.now() - timedelta(minutes=1)))
        out = io.StringIO()
        call_command("patient_access_run_jobs", stdout=out)
        assert "ran 1 job" in out.getvalue()
        assert module.jobs.get(j.id).status is JobStatus.FAILED_TERMINAL  # the token had expired


def test_a_key_list_rotates():
    from cryptography.fernet import Fernet

    old = Crypto(parse(module_config()).encryption_keys)
    token = old.encrypt("secret")
    rotated = Crypto(parse(module_config(ENCRYPTION_KEYS=[Fernet.generate_key().decode(), TEST_KEY])).encryption_keys)
    assert rotated.decrypt(token) == "secret"


def test_the_http_librarys_request_lines_stay_out_of_the_log_unless_switched_on(caplog):
    """urllib3 logs each request line at DEBUG, and a search URL carries the EHR patient id."""
    line = '"GET /fhir/Observation?category=laboratory&patient=ehr-p1 HTTP/1.1" 200'
    caplog.set_level(logging.DEBUG)
    with override_settings(PATIENT_ACCESS=module_config()):
        logging.getLogger("urllib3.connectionpool").debug(line)
    assert "ehr-p1" not in caplog.text
    with override_settings(PATIENT_ACCESS=module_config(HTTP_DEBUG_LOGGING=True)):
        logging.getLogger("urllib3.connectionpool").debug(line)
    assert "ehr-p1" in caplog.text  # local debugging: the host's level decides
    with override_settings(PATIENT_ACCESS=module_config()):
        pass  # leave the loggers pinned for the rest of the run


def test_a_crash_on_an_unloadable_row_logs_where_not_what(crypto, caplog):
    """A validation error quotes the input it rejected; the log gets the field path only."""
    from patient_access.core.runner import JobRunner

    store = OrmJobStore(HostAttrs, crypto)
    j = store.create(job(job_id=""))
    body = dict(JobRecord.objects.get(id=j.id).body)
    body["source"] = dict(body["source"], ehr_patient_id={"name": "Jane Quinn Example"})
    JobRecord.objects.filter(id=j.id).update(body=body)
    with caplog.at_level(logging.DEBUG):
        JobRunner(store, lambda lease: store.get(lease.job_id), worker=lambda: "w").run(j.id)
    assert "ValidationError" in caplog.text and "source.ehr_patient_id" in caplog.text
    assert "Jane" not in caplog.text and "Quinn" not in caplog.text
    assert JobRecord.objects.get(id=j.id).crashes == 1


# --- the admin -----------------------------------------------------------------------------


class _User:
    """A request user as the admin sees it: `has_perm` is all the base check asks."""

    is_active = True
    is_staff = True
    is_superuser = False

    def __init__(self, allowed: bool) -> None:
        self.allowed = allowed

    def has_perm(self, perm: str) -> bool:
        return self.allowed


def test_the_admin_lets_only_a_user_with_the_permission_delete_and_never_a_running_job(crypto):
    """Django's own permission check first, then our narrowing. (In JHE a staff user who is not
    a superuser holds no model permissions, so `has_perm` is stubbed rather than granted.)"""
    from django.contrib.admin.sites import AdminSite
    from django.test import RequestFactory

    from patient_access.django_app.admin import JobRecordAdmin

    store = OrmJobStore(HostAttrs, crypto)
    done = JobRecord.objects.get(id=store.create(job(job_id="", status=JobStatus.DONE)).id)
    running = JobRecord.objects.get(id=store.create(job(job_id="", status=JobStatus.FETCHING)).id)
    model_admin = JobRecordAdmin(JobRecord, AdminSite())
    request = RequestFactory().get("/")
    request.user = _User(allowed=False)
    assert model_admin.has_delete_permission(request) is False
    assert model_admin.has_delete_permission(request, done) is False
    request.user = _User(allowed=True)
    assert model_admin.has_delete_permission(request, done) is True
    assert model_admin.has_delete_permission(request, running) is False


def test_the_attempts_admin_page_never_carries_a_live_state(crypto):
    from django.test import Client
    from django.urls import reverse

    from core.models import JheUser

    a = attempt(expires_at=timezone.now() + timedelta(minutes=10))
    OrmAuthAttemptStore(LaunchAttrs, crypto).create(a)
    superuser = Client()
    superuser.force_login(JheUser.objects.create_superuser(email="root@example.org", password="pw"))
    page = superuser.get(reverse("admin:pa_jobs_authattemptrecord_changelist"))
    assert page.status_code == 200
    assert a.state not in page.content.decode()
