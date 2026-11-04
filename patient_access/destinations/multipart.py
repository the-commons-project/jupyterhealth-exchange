"""MultipartUploader: a byte stream to one S3 object, one part in memory at a time.

Explicit `create_multipart_upload` / `upload_part` / `complete_multipart_upload`, sequential, not
boto3's transfer manager: completion returns the ETag and version id the receipt records, and
peak memory is about two parts: the fixed buffer and the copy handed to botocore (the transfer
manager buffers a part per thread for a non-seekable stream). The object does not exist until
`complete`, so its existence is the "complete" marker;
any exception aborts the upload. Knows boto3's client API and nothing about jobs or FHIR.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Protocol

MiB = 1024 * 1024
MIN_PART = 5 * MiB  # S3's minimum for every part but the last
DEFAULT_PART = 32 * MiB  # 10,000 parts x 32 MiB = 320 GiB per object
MAX_PARTS = 10_000


class S3Client(Protocol):
    """The calls the uploader makes on a boto3 S3 client (boto3 is untyped; this is the seam)."""

    def create_multipart_upload(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def upload_part(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def complete_multipart_upload(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def abort_multipart_upload(self, **kwargs: Any) -> Mapping[str, Any]: ...


class TooManyParts(Exception):
    pass


class MultipartUploader:
    def __init__(
        self,
        client: S3Client,
        *,
        bucket: str,
        key: str,
        content_type: str,
        sse: Mapping[str, str],
        part_size: int = DEFAULT_PART,
    ) -> None:
        if part_size < MIN_PART:
            raise ValueError(f"part size must be at least {MIN_PART} bytes")
        self.client = client
        self.bucket, self.key, self.content_type = bucket, key, content_type
        self.sse = dict(sse)
        self.part_size = part_size
        self.upload_id: str | None = None
        self.parts: list[dict[str, str | int]] = []

    def start(self) -> str:
        r = self.client.create_multipart_upload(
            Bucket=self.bucket, Key=self.key, ContentType=self.content_type, **self.sse
        )
        self.upload_id = str(r["UploadId"])
        return self.upload_id

    def _put(self, body: bytes) -> None:
        number = len(self.parts) + 1
        if number > MAX_PARTS:
            raise TooManyParts(f"more than {MAX_PARTS} parts of {self.part_size} bytes")
        r = self.client.upload_part(
            Bucket=self.bucket, Key=self.key, UploadId=self.upload_id, PartNumber=number, Body=body
        )
        self.parts.append({"PartNumber": number, "ETag": str(r["ETag"])})

    def upload(self, chunks: Iterable[bytes]) -> int:
        """Send every chunk, grouped into parts. Returns the number of parts.

        One preallocated buffer of exactly one part, filled in place: growing and trimming a
        buffer instead makes the allocator hand out a new block per part, and a 1 GiB upload
        then peaks at over a gigabyte of resident memory (measured)."""
        if self.upload_id is None:
            raise ValueError("start() first")
        buf = bytearray(self.part_size)
        filled = 0
        for chunk in chunks:
            view = memoryview(chunk)
            while view:
                n = min(len(view), self.part_size - filled)
                buf[filled : filled + n] = view[:n]
                filled += n
                view = view[n:]
                if filled == self.part_size:
                    self._put(bytes(buf))
                    filled = 0
        if filled or not self.parts:
            with memoryview(buf) as whole:
                self._put(bytes(whole[:filled]))  # the last part may be small; an empty stream is one part
        return len(self.parts)

    def complete(self) -> tuple[str, str | None]:
        """Returns (ETag, version id when the bucket is versioned)."""
        r = self.client.complete_multipart_upload(
            Bucket=self.bucket, Key=self.key, UploadId=self.upload_id, MultipartUpload={"Parts": self.parts}
        )
        version = r.get("VersionId")
        return str(r["ETag"]), str(version) if version else None

    def abort(self) -> None:
        if self.upload_id is not None:
            self.client.abort_multipart_upload(Bucket=self.bucket, Key=self.key, UploadId=self.upload_id)
            self.upload_id = None
