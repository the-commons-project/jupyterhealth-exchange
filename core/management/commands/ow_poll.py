"""``manage.py ow_poll`` — ingest wearable data for connected patients.

Thin wrapper over ``core.services.ow_ingest.ingest_for_user``. The active
pipeline (normalized OW API or raw MinIO S3) is selected by the
``OW_PIPELINE_MODE`` env var. Reads ``ow.ingest_mode`` JheSetting and
no-ops if not "polling".

Flags:
    --patient-id <pk>   Ingest only this patient (writes OWPollEvent.trigger="manual").
    --dry-run           Read and shim, but write nothing to the DB.
    --force-backfill    Use the full backfill window regardless of last_poll_at.

The crontab in deploy/crontab fires this command once per 24 hours.
"""

import logging
import os
import time

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from core.models import Patient
from core.services.ow_ingest import build_polling_set, ingest_for_user
from core.services.ow_integration import load_and_validate_polling_config

logger = logging.getLogger(__name__)

# Sequential processing with a small inter-patient sleep to avoid
# hammering the data source (OW API or MinIO) on the first cron tick
# after deploy when every patient triggers a full backfill.
# Configurable via env var for ops tuning.
DEFAULT_INTER_PATIENT_SLEEP_SECONDS = 0.5


def _inter_patient_sleep_seconds() -> float:
    raw = os.environ.get("OW_POLL_INTER_PATIENT_SLEEP_SECONDS")
    if not raw:
        return DEFAULT_INTER_PATIENT_SLEEP_SECONDS
    try:
        v = float(raw)
        return max(0.0, v)
    except ValueError:
        logger.warning(
            "OW_POLL_INTER_PATIENT_SLEEP_SECONDS=%r is not a valid float; using default %s",
            raw,
            DEFAULT_INTER_PATIENT_SLEEP_SECONDS,
        )
        return DEFAULT_INTER_PATIENT_SLEEP_SECONDS


class Command(BaseCommand):
    help = "Ingest wearable data for connected patients (pipeline mode set by OW_PIPELINE_MODE)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--patient-id",
            type=int,
            default=None,
            help="Poll only this patient (Patient PK). Default: poll the full set.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Pull and shim, but don't write to the DB.",
        )
        parser.add_argument(
            "--force-backfill",
            action="store_true",
            help="Treat every (patient, data_type) as initial backfill.",
        )

    def handle(self, *args, patient_id=None, dry_run=False, force_backfill=False, **kwargs):
        cfg = load_and_validate_polling_config()
        if cfg["ingest_mode"] != "polling":
            logger.info("ow_poll skipped: ingest_mode=%s", cfg["ingest_mode"])
            self.stdout.write(f"ow_poll skipped: ingest_mode={cfg['ingest_mode']}")
            return

        started = timezone.now()
        if patient_id is not None:
            try:
                patients = [Patient.objects.get(pk=patient_id)]
            except Patient.DoesNotExist:
                raise CommandError(f"Patient {patient_id} does not exist") from None
            trigger = "manual"
        else:
            patients = list(build_polling_set())
            trigger = "cron"

        totals = {
            "patients_processed": 0,
            "records_ingested": 0,
            "records_skipped": 0,
            "patients_errored": 0,
        }

        for patient in patients:
            try:
                ingested, skipped, errored = ingest_for_user(
                    patient.id,
                    trigger=trigger,
                    dry_run=dry_run,
                    force_backfill=force_backfill,
                )
                totals["patients_processed"] += 1
                totals["records_ingested"] += ingested
                totals["records_skipped"] += skipped
                if errored:
                    totals["patients_errored"] += 1
            except Exception as e:
                logger.exception("ow_poll: unexpected failure for patient %s: %s", patient.id, e)
                totals["patients_errored"] += 1

            # Don't sleep on single-patient runs (operator wants immediate result).
            if patient_id is None:
                time.sleep(_inter_patient_sleep_seconds())

        duration = (timezone.now() - started).total_seconds()
        logger.info("ow_poll.summary", extra={**totals, "duration_seconds": duration})
        self.stdout.write(f"ow_poll done: {totals} duration={duration:.1f}s")
