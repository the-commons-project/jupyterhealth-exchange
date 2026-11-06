"""S3Sink: the rendered bundle stored as one object in the deployment's bucket.

The client is built per delivery from the settings snapshot (credentials are runtime settings,
so it cannot be built at boot), through a `client_factory` the host may replace. The key comes
from the layout template in the same snapshot. The stream's SHA-256 goes into the receipt: S3's
own checksum of a multipart object is a checksum of part checksums, so ours is the integrity
record a reader verifies.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from typing import Generic

from botocore.exceptions import ClientError

from patient_access.core.interfaces import DeliveryProgress
from patient_access.core.models import A, Job, PendingUpload, S3Receipt
from patient_access.core.settings import S3Settings, SseMode
from patient_access.formatters.stream import Digest

from .layout import TemplateLayout
from .multipart import DEFAULT_PART, MultipartUploader, S3Client

FHIR_JSON = "application/fhir+json"
NO_SUCH_UPLOAD = "NoSuchUpload"

log = logging.getLogger(__name__)

ClientFactory = Callable[[S3Settings], S3Client]


def boto3_client(settings: S3Settings) -> S3Client:
    """The default factory. No keys: boto3's default chain (the compute's IAM role on AWS).
    Path-style addressing for a custom endpoint (MinIO, Silo); AWS itself prefers virtual-hosted."""
    import boto3
    from botocore.config import Config

    client: S3Client = boto3.client(
        "s3",
        endpoint_url=settings.endpoint_url,
        region_name=settings.region,
        aws_access_key_id=settings.access_key_id,
        aws_secret_access_key=settings.secret_access_key.get_secret_value() if settings.secret_access_key else None,
        config=Config(
            s3={"addressing_style": "path" if settings.endpoint_url else "auto"},
            retries={"max_attempts": 5, "mode": "standard"},
        ),
    )
    return client


def sse_arguments(settings: S3Settings) -> Mapping[str, str]:
    if settings.encryption is SseMode.BUCKET_DEFAULT:
        return {}
    args = {"ServerSideEncryption": settings.encryption.value}
    if settings.encryption is SseMode.KMS and settings.kms_key_id:
        args["SSEKMSKeyId"] = settings.kms_key_id
    return args


class S3Sink(Generic[A]):
    def __init__(self, client_factory: ClientFactory = boto3_client, part_size: int = DEFAULT_PART) -> None:
        self.client_factory = client_factory
        self.part_size = part_size

    def put(self, job: Job[A], chunks: Iterable[bytes], settings: S3Settings, progress: DeliveryProgress) -> S3Receipt:
        key = TemplateLayout(settings.layout).render(job)
        uploader = MultipartUploader(
            self.client_factory(settings),
            bucket=settings.bucket,
            key=key,
            content_type=FHIR_JSON,
            sse=sse_arguments(settings),
            part_size=self.part_size,
        )
        upload = PendingUpload(bucket=settings.bucket, key=key, upload_id=uploader.start())
        digest = Digest(chunks)
        try:
            # Recorded first, inside the try: if recording fails, the upload is still aborted.
            progress.started(upload)
            parts = uploader.upload(digest)
            etag, version = uploader.complete()
        except BaseException:
            # The original error is what the caller sees. An abort that fails is logged and the
            # upload stays recorded on the job, so the next attempt aborts it; only an abort
            # that succeeded clears the record.
            try:
                uploader.abort()
            except Exception as abort_error:  # noqa: BLE001 - must not replace the original error
                log.warning("abort of an upload failed (%s); left for the next attempt", type(abort_error).__name__)
            else:
                progress.ended(upload)
            raise
        progress.ended(upload)
        return S3Receipt(
            bucket=settings.bucket,
            key=key,
            etag=etag.strip('"'),
            version_id=version,
            bytes=digest.size,
            sha256=digest.sha256,
            parts=parts,
        )

    def abort(self, upload: PendingUpload, settings: S3Settings) -> None:
        client = self.client_factory(settings)
        try:
            client.abort_multipart_upload(Bucket=upload.bucket, Key=upload.key, UploadId=upload.upload_id)
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") != NO_SUCH_UPLOAD:  # already gone is the goal
                raise
