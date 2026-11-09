"""`manage.py check`: say when the module is off. A broken configuration already failed startup."""

from __future__ import annotations

from typing import Any

from django.core.checks import CheckMessage, Info, register

from .conf import enabled, project_settings


@register()
def patient_access_configured(app_configs: Any, **kwargs: Any) -> list[CheckMessage]:
    if enabled(project_settings()):
        return []
    return [
        Info(
            "USCDI Patient Access is off: PATIENT_ACCESS has no ENCRYPTION_KEYS.",
            hint="Set PATIENT_ACCESS_ENCRYPTION_KEYS to turn it on.",
            id="patient_access.I001",
        )
    ]
