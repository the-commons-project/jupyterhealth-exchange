"""Boto3 wrapper for reading raw Oura payloads from MinIO."""

import json
from datetime import datetime
from typing import NamedTuple

import boto3

from core.jhe_settings.service import get_setting


class S3ObjectInfo(NamedTuple):
    key: str
    last_modified: datetime
    metadata: dict[str, str]


def get_client():
    """Create a boto3 S3 client from current get_setting() values.

    Not cached — settings are operator-configurable at runtime.
    boto3 client creation is cheap.
    """
    return boto3.client(
        "s3",
        endpoint_url=get_setting("s3.endpoint_url", "http://ow-object-store:9000"),
        aws_access_key_id=get_setting("s3.access_key_id", "minioadmin"),
        aws_secret_access_key=get_setting("s3.secret_access_key", "minioadmin"),
    )


def list_new_objects(ow_user_id: str, since: datetime) -> list[S3ObjectInfo]:
    """List S3 objects for a user newer than ``since``.

    Calls head_object per candidate to read trace_id metadata (list_objects_v2
    does not return user metadata).
    """
    client = get_client()
    bucket = get_setting("s3.bucket_name", "raw-payloads")
    prefix = get_setting("s3.key_prefix", "raw-payloads/oura/api_response")

    results: list[S3ObjectInfo] = []
    continuation_token = None

    while True:
        kwargs = {"Bucket": bucket, "Prefix": prefix}
        if continuation_token:
            kwargs["ContinuationToken"] = continuation_token

        response = client.list_objects_v2(**kwargs)

        for obj in response.get("Contents", []):
            key = obj["Key"]
            last_modified = obj["LastModified"]

            if f"/{ow_user_id}/" not in key:
                continue
            if last_modified <= since:
                continue

            head = client.head_object(Bucket=bucket, Key=key)
            results.append(S3ObjectInfo(key=key, last_modified=last_modified, metadata=head.get("Metadata", {})))

        if not response.get("IsTruncated"):
            break
        continuation_token = response.get("NextContinuationToken")

    return results


def read_object(key: str) -> dict:
    """Download an S3 object and parse its JSON body."""
    client = get_client()
    bucket = get_setting("s3.bucket_name", "raw-payloads")
    response = client.get_object(Bucket=bucket, Key=key)
    return json.loads(response["Body"].read())
