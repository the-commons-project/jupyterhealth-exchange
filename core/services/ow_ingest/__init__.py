"""OW ingestion pipeline dispatcher.

Reads ``OW_PIPELINE_MODE`` from Django settings and re-exports the
correct orchestrator's public API. Callers import from this package:

    from core.services.ow_ingest import ingest_for_user, build_polling_set, DATA_TYPES
"""

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from core.services.ow_ingest._common import build_polling_set

_mode = getattr(settings, "OW_PIPELINE_MODE", "normalized")

if _mode == "raw":
    from core.services.ow_ingest.orchestrator_raw import (
        DATA_TYPES,
        ingest_for_user,
    )
elif _mode == "normalized":
    from core.services.ow_ingest.orchestrator_normalized import (
        DATA_TYPES,
        ingest_for_user,
    )
else:
    raise ImproperlyConfigured(f"OW_PIPELINE_MODE must be 'normalized' or 'raw', got '{_mode}'")

__all__ = ["ingest_for_user", "build_polling_set", "DATA_TYPES"]
