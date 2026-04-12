"""Tests for the OW ingest dispatcher (__init__.py)."""

import importlib

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings


@pytest.mark.django_db
class TestDispatcher:
    """Verify that OW_PIPELINE_MODE routes to the correct orchestrator.

    These tests use ``importlib.reload`` because the dispatcher evaluates
    the mode at import time. ``override_settings`` changes the setting but
    the module has already been imported, so we must reload to re-evaluate
    the branch. This is inherently fragile but is the only way to test
    import-time dispatch without subprocess isolation.
    """

    @override_settings(OW_PIPELINE_MODE="normalized")
    def test_normalized_mode_selects_normalized_orchestrator(self):
        import core.services.ow_ingest as pkg

        importlib.reload(pkg)
        from core.services.ow_ingest import orchestrator_normalized

        assert pkg.ingest_for_user is orchestrator_normalized.ingest_for_user
        assert pkg.DATA_TYPES is orchestrator_normalized.DATA_TYPES

    @override_settings(OW_PIPELINE_MODE="raw")
    def test_raw_mode_selects_raw_orchestrator(self):
        import core.services.ow_ingest as pkg

        importlib.reload(pkg)
        from core.services.ow_ingest import orchestrator_raw

        assert pkg.ingest_for_user is orchestrator_raw.ingest_for_user
        assert pkg.DATA_TYPES is orchestrator_raw.DATA_TYPES

    @override_settings(OW_PIPELINE_MODE="normalized")
    def test_build_polling_set_always_from_common(self):
        import core.services.ow_ingest as pkg

        importlib.reload(pkg)
        from core.services.ow_ingest._common import (
            build_polling_set as common_build_polling_set,
        )

        assert pkg.build_polling_set is common_build_polling_set

    @override_settings(OW_PIPELINE_MODE="invalid")
    def test_invalid_mode_raises_improperly_configured(self):
        import core.services.ow_ingest as pkg

        with pytest.raises(ImproperlyConfigured, match="must be 'normalized' or 'raw'"):
            importlib.reload(pkg)
