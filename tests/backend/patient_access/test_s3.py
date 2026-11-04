"""The multipart uploader and the S3 sink against moto: the object exists only once complete, a
failure leaves neither an object nor parts, and the receipt matches what S3 holds."""

import hashlib
import os

import boto3
import pytest
from moto import mock_aws

from patient_access.core.models import PendingUpload
from patient_access.destinations.multipart import MIN_PART, MultipartUploader
from patient_access.destinations.s3 import S3Sink, boto3_client

from .helpers import job, settings

BUCKET = "pulls"
KEY = "40001/7/20261104T120000Z/bundle.json"


@pytest.fixture
def s3(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1", aws_access_key_id="k", aws_secret_access_key="s")
        client.create_bucket(Bucket=BUCKET)
        yield client


def s3_settings(**kw):
    return settings(access_key_id="k", secret_access_key="s", region="us-east-1", **kw).s3


class Progress:
    def __init__(self):
        self.events = []

    def started(self, upload):
        self.events.append(("started", upload))

    def ended(self):
        self.events.append(("ended",))


def pieces(total: int, size: int = 1024 * 1024):
    block = os.urandom(size)
    sent = 0
    while sent < total:
        n = min(size, total - sent)
        yield block[:n]
        sent += n


def test_a_multi_part_object_lands_whole_with_a_matching_receipt(s3):
    s3.put_bucket_versioning(Bucket=BUCKET, VersioningConfiguration={"Status": "Enabled"})
    data = b"".join(pieces(2 * MIN_PART + 123))
    progress = Progress()
    receipt = S3Sink(boto3_client, part_size=MIN_PART).put(
        job(), [data[i : i + 999_999] for i in range(0, len(data), 999_999)], s3_settings(), progress
    )
    head = s3.head_object(Bucket=BUCKET, Key=KEY)
    assert receipt.key == KEY and receipt.parts == 3 and receipt.bytes == len(data)
    assert receipt.sha256 == hashlib.sha256(data).hexdigest()
    assert receipt.etag == head["ETag"].strip('"') and receipt.version_id == head["VersionId"]
    assert head["ServerSideEncryption"] == "AES256" and head["ContentType"] == "application/fhir+json"
    assert s3.get_object(Bucket=BUCKET, Key=KEY)["Body"].read() == data
    assert [e[0] for e in progress.events] == ["started", "ended"]
    assert receipt.location == f"s3://{BUCKET}/{KEY}"


def test_a_small_bundle_is_a_single_part_upload(s3):
    receipt = S3Sink(boto3_client).put(job(), [b'{"resourceType":"Bundle"}'], s3_settings(), Progress())
    assert receipt.parts == 1 and s3.get_object(Bucket=BUCKET, Key=KEY)["Body"].read() == b'{"resourceType":"Bundle"}'


def test_a_failure_mid_stream_leaves_no_object_and_no_parts(s3):
    def failing():
        yield from pieces(MIN_PART + 10)
        raise RuntimeError("the spool went away")

    progress = Progress()
    with pytest.raises(RuntimeError):
        S3Sink(boto3_client, part_size=MIN_PART).put(job(), failing(), s3_settings(), progress)
    assert s3.list_objects_v2(Bucket=BUCKET).get("KeyCount") == 0
    assert not s3.list_multipart_uploads(Bucket=BUCKET).get("Uploads")
    assert progress.events[-1] == ("ended",)


def test_an_upload_a_dead_process_left_open_can_be_aborted(s3):
    up = MultipartUploader(s3, bucket=BUCKET, key=KEY, content_type="application/fhir+json", sse={})
    upload_id = up.start()
    sink = S3Sink(boto3_client)
    sink.abort(PendingUpload(bucket=BUCKET, key=KEY, upload_id=upload_id), s3_settings())
    assert not s3.list_multipart_uploads(Bucket=BUCKET).get("Uploads")
    sink.abort(PendingUpload(bucket=BUCKET, key=KEY, upload_id=upload_id), s3_settings())  # already gone: fine


def test_kms_encryption_is_requested_when_configured(s3):
    sink = S3Sink(boto3_client)
    kms = boto3.client("kms", region_name="us-east-1", aws_access_key_id="k", aws_secret_access_key="s")
    key_id = kms.create_key()["KeyMetadata"]["KeyId"]
    sink.put(job(), [b"{}"], s3_settings(encryption="aws:kms", kms_key_id=key_id), Progress())
    head = s3.head_object(Bucket=BUCKET, Key=KEY)
    assert head["ServerSideEncryption"] == "aws:kms" and key_id in head["SSEKMSKeyId"]


def test_the_part_size_has_a_floor():
    with pytest.raises(ValueError):
        MultipartUploader(object(), bucket="b", key="k", content_type="c", sse={}, part_size=1024)
