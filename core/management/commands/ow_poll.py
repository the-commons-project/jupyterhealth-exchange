import json
import logging
import sys
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone
from minio import Minio

from core.jhe_settings.service import get_setting
from core.models import CodeableConcept, DataSource, Observation, ObservationIdentifier, PatientWearableConnection

logger = logging.getLogger(__name__)

POLL_OVERLAP = timedelta(minutes=5)
POLL_WINDOW = timedelta(days=30)


class Command(BaseCommand):
    help = "Poll Open Wearables for new heart rate data"

    def add_arguments(self, parser):
        parser.add_argument(
            "--patient-id",
            type=int,
            default=None,
            help="Poll only the specified patient (by Patient.id). For debugging/backfill.",
        )

    def handle(self, *args, **options):
        mode = get_setting("ow.ingest_mode", "normalized")
        patient_id = options.get("patient_id")
        self.stdout.write(f"OW poll starting (mode={mode}{' patient_id=' + str(patient_id) if patient_id else ''})")

        connections = PatientWearableConnection.objects.all()
        if patient_id is not None:
            connections = connections.filter(patient_id=patient_id)
        if not connections.exists():
            self.stdout.write("No wearable connections found, nothing to poll.")
            return

        data_source = DataSource.objects.filter(name="Oura Ring").first()
        if not data_source:
            self.stderr.write("DataSource 'Oura Ring' not found. Run seed or create it manually.")
            return

        codeable_concept = CodeableConcept.objects.filter(coding_code="omh:heart-rate:2.0").first()
        if not codeable_concept:
            self.stderr.write("CodeableConcept 'omh:heart-rate:2.0' not found. Run seed or create it manually.")
            return

        total_created = 0
        for conn in connections:
            if "heart_rate" not in conn.consented_scopes:
                continue
            consented_ids = {s.id for s in conn.patient.consolidated_consented_scopes()}
            if codeable_concept.id not in consented_ids:
                logger.warning(f"Skipping patient {conn.patient_id}: no StudyPatientScopeConsent for heart rate")
                continue
            poll_started_at = timezone.now()
            try:
                count = self._poll_connection(conn, data_source, codeable_concept, mode)
                total_created += count
                logger.info(f"Poll completed for patient={conn.patient_id} connection={conn.id} created={count}")
                PatientWearableConnection.objects.filter(pk=conn.pk).update(last_polled_at=poll_started_at)
            except Exception:
                logger.exception(f"Failed to poll connection {conn.id} (patient={conn.patient_id})")

        self.stdout.write(self.style.SUCCESS(f"OW poll complete. Created {total_created} observations."))

    def _poll_connection(self, conn, data_source, codeable_concept, mode):
        if mode == "normalized":
            return self._poll_normalized(conn, data_source, codeable_concept)
        elif mode == "raw":
            return self._poll_raw(conn, data_source, codeable_concept)
        else:
            self.stderr.write(f"Unknown ingest mode: {mode}")
            return 0

    def _poll_normalized(self, conn, data_source, codeable_concept):
        import core.ow_client as ow_client

        end_time = timezone.now()
        start_time = end_time - POLL_WINDOW
        if conn.last_polled_at:
            start_time = max(start_time, conn.last_polled_at - POLL_OVERLAP)

        records = ow_client.get_heart_rate_data(conn.ow_user_id, start_time.isoformat(), end_time.isoformat())
        logger.info(f"Fetched {len(records)} records for patient={conn.patient_id} connection={conn.id}")
        return self._ingest_records(records, conn, data_source, codeable_concept, source="ow_normalized")

    def _poll_raw(self, conn, data_source, codeable_concept):
        # TODO(scale): Lists every object in the bucket under the oura/api_response
        # prefix and filters client-side by ow_user_id and trace_id. At scale this
        # is O(N) per poll. OW's S3 key structure should be changed to include
        # ow_user_id in the prefix so we can list by user.
        endpoint = get_setting("s3.endpoint_url", "localhost:9000")
        access_key = get_setting("s3.access_key_id", "")
        secret_key = get_setting("s3.secret_access_key", "")
        bucket = get_setting("s3.bucket_name", "raw-payloads")
        secure = get_setting("s3.use_ssl", False)

        client = Minio(endpoint, access_key=access_key, secret_key=secret_key, secure=secure)
        key_prefix = get_setting("s3.key_prefix", "raw-payloads")
        prefix = f"{key_prefix}/oura/api_response/"

        records = []
        for obj in client.list_objects(bucket, prefix=prefix, recursive=True):
            if conn.ow_user_id not in obj.object_name:
                continue
            try:
                meta = client.stat_object(bucket, obj.object_name).metadata
                if meta.get("x-amz-meta-trace_id") != "/v2/usercollection/heartrate":
                    continue

                response = client.get_object(bucket, obj.object_name)
                try:
                    payload = json.loads(response.read())
                finally:
                    response.close()
                    response.release_conn()

                if not isinstance(payload, dict):
                    logger.warning(f"Skipping non-dict payload at {obj.object_name}")
                    continue
                data = payload.get("data", [])
                if not isinstance(data, list):
                    logger.warning(f"Skipping payload with non-list data at {obj.object_name}")
                    continue
                records.extend(data)
            except Exception:
                logger.warning(f"Failed to read S3 object {obj.object_name}", exc_info=True)
                continue

        logger.info(f"Fetched {len(records)} records for patient={conn.patient_id} connection={conn.id}")
        return self._ingest_records(records, conn, data_source, codeable_concept, source="oura_raw")

    def _ingest_records(self, records, conn, data_source, codeable_concept, source):
        omh_shim = sys.modules.get("omh_shim")
        if omh_shim is None:
            import omh_shim

        created = 0
        for record in records:
            try:
                omh_record = omh_shim.convert(source=source, data_type="heart_rate", sample=record)
            except Exception:
                logger.warning(f"Skipping unconvertible record for patient {conn.patient_id}", exc_info=True)
                continue

            identifier_value = omh_record["header"].get("uuid", record.get("timestamp", ""))
            if not identifier_value:
                continue
            try:
                with transaction.atomic():
                    if ObservationIdentifier.objects.filter(system="omh-shim", value=identifier_value).exists():
                        continue
                    observation = Observation.objects.create(
                        subject_patient=conn.patient,
                        data_source=data_source,
                        codeable_concept=codeable_concept,
                        status="final",
                        value_attachment_data=omh_record,
                    )
                    ObservationIdentifier.objects.create(
                        observation=observation,
                        system="omh-shim",
                        value=identifier_value,
                    )
                created += 1
            except Exception:
                logger.warning(
                    f"Failed to persist observation for patient {conn.patient_id} (identifier={identifier_value})",
                    exc_info=True,
                )
                continue

        return created
