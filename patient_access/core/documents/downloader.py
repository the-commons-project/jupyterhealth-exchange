"""One candidate into the spool, streamed: `Stored` with the blob's metadata, or `Skipped` with
the reason. Memory stays flat at any document size: bytes go from the response through the
spool's encrypting `BlobWriter` to disk in fixed chunks.

Raw bytes are asked for with `Accept`. A server that answers with a FHIR `Binary` JSON instead is
parsed in memory, so that answer has its own, lower cap; the response buffer is released as soon
as it is parsed and the base64 is decoded slice by slice, so the peak is the buffer plus the
parsed string, briefly. Either way the spool holds the document's raw bytes. The full served
Content-Type is kept (a charset matters to whoever decodes the text); only its media type decides
whether a body is JSON. A download that runs past `max_seconds_each` is abandoned. The token is
never sent to another origin. TokenExpired and Unauthorized propagate: the pass decides what
they mean.
"""

from __future__ import annotations

import base64
import binascii
import json
import time
from collections.abc import Callable, Iterable, Iterator

import requests

from ..client import FhirClient
from ..interfaces import BlobWriter, SpoolHandle
from ..models import BlobMeta, SkipReason, Value
from ..settings import DocumentCaps
from .budget import Budget
from .candidates import Candidate, media_type

CHUNK = 64 * 1024
_B64_SLICE = 4 * CHUNK // 3 // 4 * 4  # base64 characters per decoded chunk, a multiple of 4
_JSON_TYPES = frozenset({"application/fhir+json", "application/json"})
OCTET_STREAM = "application/octet-stream"


class Stored(Value):
    meta: BlobMeta


class Skipped(Value):
    reason: SkipReason
    detail: str | None = None


FetchResult = Stored | Skipped


class _OverLimit(Exception):
    def __init__(self, size: int) -> None:
        self.size = size


class _TooSlow(Exception):
    pass


def _decoded_inline(data: str) -> Iterator[bytes]:
    """Inline base64 decoded slice by slice, skipping whitespace (line-wrapped base64) as it
    goes, without a whitespace-free copy of the whole text. Raises binascii.Error on bad data."""
    carry = ""
    for i in range(0, len(data), _B64_SLICE):
        piece = carry + "".join(data[i : i + _B64_SLICE].split())
        cut = len(piece) - len(piece) % 4
        if cut:
            yield base64.b64decode(piece[:cut], validate=True)
        carry = piece[cut:]
    if carry:
        yield base64.b64decode(carry, validate=True)


def _write_all(writer: BlobWriter, chunks: Iterable[bytes], limit: int, overdue: Callable[[], bool]) -> None:
    for chunk in chunks:
        if writer.size + len(chunk) > limit:
            raise _OverLimit(writer.size + len(chunk))
        if overdue():
            raise _TooSlow()
        writer.write(chunk)


def _store(
    spool: SpoolHandle,
    cand: Candidate,
    chunks: Iterable[bytes],
    content_type: str,
    budget: Budget,
    source_url: str | None,
    overdue: Callable[[], bool] = lambda: False,
) -> FetchResult:
    writer = spool.open_blob(cand.blob_id)
    try:
        _write_all(writer, chunks, budget.limit, overdue)
    except _OverLimit as e:
        writer.abort()
        return Skipped(reason=budget.refusal(e.size) or SkipReason.SIZE_CAP_EACH)
    except _TooSlow:
        writer.abort()
        return Skipped(reason=SkipReason.TOO_SLOW)
    except binascii.Error:
        writer.abort()
        return Skipped(reason=SkipReason.BAD_INLINE_DATA)
    except BaseException:
        writer.abort()
        raise
    return Stored(
        meta=writer.commit(content_type=content_type, referenced_by=cand.referenced_by, source_url=source_url)
    )


def _binary_json(
    spool: SpoolHandle,
    cand: Candidate,
    response: requests.Response,
    budget: Budget,
    cap: int,
    served: str,
    overdue: Callable[[], bool],
) -> FetchResult:
    buf = bytearray()
    for chunk in response.iter_content(CHUNK):
        buf += chunk
        if len(buf) > cap:
            return Skipped(reason=SkipReason.BINARY_JSON_CAP)
        if overdue():
            return Skipped(reason=SkipReason.TOO_SLOW)
    try:
        body = json.loads(buf)  # a bytearray parses as is: no copy of the response
    except ValueError:
        return Skipped(reason=SkipReason.UNREADABLE_BINARY)
    if not (isinstance(body, dict) and body.get("resourceType") == "Binary"):
        # A JSON document in its own right (rare): keep the bytes as served.
        return _store(spool, cand, [bytes(buf)], served, budget, cand.url, overdue)
    del buf  # parsed: from here only the base64 string is held, decoded a slice at a time
    data = body.get("data")
    ctype = body.get("contentType")
    content_type = ctype if isinstance(ctype, str) and ctype else cand.content_type or OCTET_STREAM
    if not isinstance(data, str):
        return Skipped(reason=SkipReason.UNREADABLE_BINARY)
    return _store(spool, cand, _decoded_inline(data), content_type, budget, cand.url)


def fetch_one(
    client: FhirClient, spool: SpoolHandle, cand: Candidate, budget: Budget, caps: DocumentCaps
) -> FetchResult:
    if cand.data is not None:
        return _store(spool, cand, _decoded_inline(cand.data), cand.content_type or OCTET_STREAM, budget, None)
    if cand.url is None:
        return Skipped(reason=SkipReason.NO_CONTENT)
    url = client.absolute(cand.url)
    if url is None:
        return Skipped(reason=SkipReason.URL_OUTSIDE_SERVER)
    deadline = time.monotonic() + caps.max_seconds_each

    def overdue() -> bool:
        return time.monotonic() > deadline

    try:
        response = client.stream(url, cand.content_type or "*/*")
    except requests.RequestException as e:
        return Skipped(reason=SkipReason.NETWORK, detail=type(e).__name__)
    with response:
        if response.status_code != 200:
            return Skipped(reason=SkipReason.HTTP_ERROR, detail=str(response.status_code))
        declared = response.headers.get("Content-Length")
        if declared and declared.isdigit() and (refusal := budget.refusal(int(declared))):
            return Skipped(reason=refusal)
        served = response.headers.get("Content-Type") or cand.content_type or OCTET_STREAM
        try:
            if media_type(served) in _JSON_TYPES:
                return _binary_json(spool, cand, response, budget, caps.max_binary_json_bytes, served, overdue)
            return _store(spool, cand, response.iter_content(CHUNK), served, budget, cand.url, overdue)
        except requests.RequestException as e:
            return Skipped(reason=SkipReason.NETWORK, detail=type(e).__name__)
