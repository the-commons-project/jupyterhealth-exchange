"""The Django app: the ORM stores (encryption at rest, single use, leases), the config, the
wiring, and the cron command."""

import io
from datetime import timedelta

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone

from patient_access.core.models import JobStatus
from patient_access.django_app.conf import ConfigError, parse
from patient_access.django_app.crypto import Crypto
from patient_access.django_app.models import AuthAttemptRecord, JobRecord
from patient_access.django_app.stores import OrmAuthAttemptStore, OrmJobStore
from patient_access.django_app.wiring import NotConfigured, build_module, get_module

from .helpers import TEST_KEY, HostAttrs, LaunchAttrs, attempt, job, module_config

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
    store.save(back)
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
    assert store.claim_next("b", lease, timedelta(0)) == j.id
    JobRecord.objects.filter(id=j.id).update(lease_expires_at=timezone.now() - timedelta(seconds=1))
    assert store.claim_next("c", lease, timedelta(0)) == j.id  # a dead worker's lease lapsed


def test_save_never_touches_the_lease(crypto):
    store = OrmJobStore(HostAttrs, crypto)
    j = store.create(job(job_id=""))
    store.claim(j.id, "a", timedelta(seconds=60))
    store.save(store.get(j.id))
    assert JobRecord.objects.get(id=j.id).lease_owner == "a"


def test_terminal_jobs_are_never_claimed_and_failed_ones_wait(crypto):
    store = OrmJobStore(HostAttrs, crypto)
    done = store.create(job(job_id=""))
    done.status = JobStatus.DONE
    store.save(done)
    assert not store.claim(done.id, "a", timedelta(seconds=60))
    failed = store.create(job(job_id=""))
    failed.status = JobStatus.FAILED
    store.save(failed)
    assert store.claim_next("a", timedelta(seconds=60), timedelta(seconds=30)) is None
    JobRecord.objects.filter(id=failed.id).update(updated_at=timezone.now() - timedelta(minutes=1))
    assert store.claim_next("a", timedelta(seconds=60), timedelta(seconds=30)) == failed.id


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
