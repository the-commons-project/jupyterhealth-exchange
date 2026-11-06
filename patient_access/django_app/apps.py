from __future__ import annotations

import atexit
import logging
from typing import Any

from django.apps import AppConfig
from django.core.exceptions import ImproperlyConfigured
from django.core.signals import setting_changed

HTTP_LIBRARY_LOGGERS = ("urllib3", "urllib3.connectionpool", "urllib3.util.retry")


def quiet_http_library(debug_logging: bool) -> None:
    """The HTTP library logs each request line, query string included, at DEBUG; a search URL
    carries the EHR patient id. Pinned to WARNING unless HTTP_DEBUG_LOGGING is on."""
    for name in HTTP_LIBRARY_LOGGERS:
        logging.getLogger(name).setLevel(logging.NOTSET if debug_logging else logging.WARNING)


def configure() -> None:
    """Build the module from PATIENT_ACCESS, or leave it off when ENCRYPTION_KEYS is empty."""
    from .conf import ConfigError, enabled, parse, project_settings
    from .wiring import build_module, install

    raw = project_settings()
    if not enabled(raw):
        install(None)
        return
    try:
        config = parse(raw)
        install(build_module(config))
    except ConfigError as e:
        raise ImproperlyConfigured(str(e)) from None
    quiet_http_library(config.http_debug_logging)


def _on_setting_changed(*, setting: str, **kwargs: Any) -> None:
    if setting == "PATIENT_ACCESS":
        configure()


class PatientAccessConfig(AppConfig):
    name = "patient_access.django_app"
    label = "pa_jobs"
    verbose_name = "Patient Access"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from . import checks  # noqa: F401 - registers the system check
        from .wiring import release_all

        configure()
        setting_changed.connect(_on_setting_changed)
        atexit.register(release_all)
