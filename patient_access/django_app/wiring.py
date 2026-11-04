"""The module, built once per process from `ModuleConfig` by the app's `ready()`.

Construction touches no database (stores query lazily) and builds no S3 client (credentials are
runtime settings), so it is safe at import time for every management command. The host's own
classes are named by dotted path in the config and checked here.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast

import requests
from django.db import connection, transaction
from django.utils.module_loading import import_string
from pydantic import BaseModel

from patient_access.core.credentials import PUBLIC_ONLY, Keyring
from patient_access.core.http import session as http_session
from patient_access.core.interfaces import Directory, JobDispatcher
from patient_access.core.models import Job, JobId
from patient_access.core.pipeline import DeliveryGuard, Deps, SettingsProvider, run_job, utcnow
from patient_access.core.runner import JobRunner
from patient_access.destinations.s3 import ClientFactory, S3Sink
from patient_access.dispatch.thread import ThreadDispatcher
from patient_access.formatters import render_bundle
from patient_access.spool.local_disk import LocalDiskSpool

from .conf import ConfigError, DispatcherKind, ModuleConfig
from .crypto import Crypto
from .stores import OrmAuthAttemptStore, OrmJobStore


class NotConfigured(Exception):
    """The module is off in this deployment (PATIENT_ACCESS has no ENCRYPTION_KEYS)."""


def close_db_connection() -> None:
    """Django opens one connection per thread; a thread the module started closes its own."""
    connection.close()


class OnCommitDispatcher:
    """Dispatch once the surrounding transaction commits: a thread started earlier would look for
    a job row it cannot see yet. Outside a transaction it dispatches at once."""

    def __init__(self, inner: JobDispatcher) -> None:
        self.inner = inner

    def dispatch(self, job_id: JobId) -> None:
        transaction.on_commit(lambda: self.inner.dispatch(job_id))


class SweepOnly:
    """DISPATCHER "none": nothing runs at dispatch; the cron sweep claims the queued row."""

    def dispatch(self, job_id: JobId) -> None:
        return None


@dataclass(frozen=True)
class Module:
    """Everything a host needs. Attribute types are the generic plugs over `BaseModel`: the host
    knows its own attrs classes (named in the config) and builds participants with them."""

    config: ModuleConfig
    launch_attrs: type[BaseModel]
    job_attrs: type[BaseModel]
    directory: Directory
    attempts: OrmAuthAttemptStore[BaseModel]
    jobs: OrmJobStore[BaseModel]
    spool: LocalDiskSpool
    keyring: Keyring
    deps: Deps[BaseModel]
    runner: JobRunner[BaseModel]
    dispatcher: JobDispatcher
    http: requests.Session
    clock: Callable[[], datetime]

    def run(self, job_id: JobId) -> Job[BaseModel]:
        return run_job(job_id, self.deps)


def _load(path: str, what: str) -> object:
    try:
        return cast(object, import_string(path))
    except ImportError as e:
        raise ConfigError(f"PATIENT_ACCESS: {what} {path!r} cannot be imported: {e}") from None


def _model(path: str, what: str) -> type[BaseModel]:
    obj = _load(path, what)
    if not (isinstance(obj, type) and issubclass(obj, BaseModel)):
        raise ConfigError(f"PATIENT_ACCESS: {what} {path!r} is not a pydantic model")
    return obj


def _callable(path: str, what: str) -> Callable[..., Any]:
    obj = _load(path, what)
    if not callable(obj):
        raise ConfigError(f"PATIENT_ACCESS: {what} {path!r} is not callable")
    return obj


def build_module(config: ModuleConfig, clock: Callable[[], datetime] = utcnow) -> Module:
    crypto = Crypto(config.encryption_keys)
    launch_attrs = _model(config.launch_attrs, "LAUNCH_ATTRS")
    job_attrs = _model(config.job_attrs, "JOB_ATTRS")
    directory = cast(Directory, _callable(config.directory, "DIRECTORY")())
    settings_provider = cast(SettingsProvider, _callable(config.settings_provider, "SETTINGS_PROVIDER"))
    delivery_guard = cast("DeliveryGuard[BaseModel]", _callable(config.delivery_guard, "DELIVERY_GUARD"))
    client_factory = cast(ClientFactory, _callable(config.s3_client_factory, "S3_CLIENT_FACTORY"))
    jobs = OrmJobStore(job_attrs, crypto)
    spool = LocalDiskSpool(config.spool_dir)
    deps = Deps[BaseModel](
        jobs=jobs,
        spool=spool,
        directory=directory,
        sink=S3Sink(client_factory, config.part_size),
        settings_provider=settings_provider,
        delivery_guard=delivery_guard,
        render=render_bundle,
        clock=clock,
        max_attempts=config.max_attempts,
    )
    runner = JobRunner(
        jobs,
        lambda job_id: run_job(job_id, deps),
        lease=timedelta(seconds=config.lease_seconds),
        on_thread_exit=close_db_connection,
    )
    inner: JobDispatcher = (
        ThreadDispatcher(runner.run, on_thread_exit=close_db_connection)
        if config.dispatcher is DispatcherKind.THREAD
        else SweepOnly()
    )
    return Module(
        config=config,
        launch_attrs=launch_attrs,
        job_attrs=job_attrs,
        directory=directory,
        attempts=OrmAuthAttemptStore(launch_attrs, crypto),
        jobs=jobs,
        spool=spool,
        keyring=PUBLIC_ONLY,  # public clients only in this build
        deps=deps,
        runner=runner,
        dispatcher=OnCommitDispatcher(inner),
        http=http_session(),
        clock=clock,
    )


_lock = threading.Lock()
_module: Module | None = None


def install(module: Module | None) -> None:
    """Set (or clear) the process's module. Called by the app's `ready()` and by tests."""
    global _module
    with _lock:
        _module = module


def get_module() -> Module:
    """The module `ready()` built. Raises NotConfigured when the module is off."""
    with _lock:
        module = _module
    if module is None:
        raise NotConfigured("USCDI Patient Access is not configured in this deployment.")
    return module


def release_all() -> None:
    """For atexit: give back every lease this process holds, so the sweep resumes at once."""
    with _lock:
        module = _module
    if module is not None:
        module.runner.release_all()
