"""Bytes out of entries, in flat memory: bundle framing, documents base64-encoded in flight, and a
digest of everything that passes. All pure: iterators in, iterators out.

An entry is either a resource, serialised whole, or a document's `Binary`, whose base64 is
streamed into the resource's `data` without ever building the string: the Binary's other fields
are serialised, and the blob is encoded straight after them. Chunks are encoded in multiples of
3 bytes, so no padding appears mid-string, and the base64 alphabet needs no JSON escaping.

The output is strict JSON whatever the EHR sent: every non-ASCII character is escaped (a lone
surrogate becomes `\\udXXX` rather than failing to encode) and a non-finite number is refused
(the puller turns the EHR's NaN into null before it is spooled).
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import Any

from patient_access.core.models import JsonObject


@dataclass(frozen=True, slots=True)
class ResourceEntry:
    """A Bundle entry carrying one FHIR resource as JSON (references rewritten). A dataclass,
    not a pydantic model: a resource can be large and must not be copied by validation."""

    full_url: str
    resource: JsonObject


@dataclass(frozen=True, slots=True)
class BinaryEntry:
    """A Bundle entry carrying one document as a FHIR `Binary`, its bytes read from the spool by
    `blob_id` and base64-encoded as they stream. A dataclass for the same reason: built once per
    document in the formatter's loop, nothing to validate."""

    full_url: str
    id: str
    content_type: str
    blob_id: str
    size: int


Entry = ResourceEntry | BinaryEntry


def dumps(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def base64_stream(chunks: Iterable[bytes]) -> Iterator[bytes]:
    """Standard base64 of the concatenated chunks, emitted as it goes. Identical to encoding the
    whole input at once: only the final group is padded."""
    carry = b""
    for chunk in chunks:
        data = carry + chunk if carry else chunk
        cut = len(data) - len(data) % 3
        if cut:
            yield base64.b64encode(data[:cut])
        carry = data[cut:]
    if carry:
        yield base64.b64encode(carry)


def entry_bytes(entry: Entry, blob: Callable[[str], Iterable[bytes]]) -> Iterator[bytes]:
    match entry:
        case ResourceEntry(full_url=full_url, resource=resource):
            yield dumps({"fullUrl": full_url, "resource": resource}).encode()
        case BinaryEntry(full_url=full_url, id=binary_id, content_type=content_type, blob_id=blob_id, size=size):
            binary = {"resourceType": "Binary", "id": binary_id, "contentType": content_type}
            text = dumps({"fullUrl": full_url, "resource": binary})
            if size == 0:  # FHIR forbids an empty string: an empty document has no data at all
                yield text.encode()
                return
            yield (text[:-2] + ',"data":"').encode()  # text ends with the two closing braces
            yield from base64_stream(blob(blob_id))
            yield b'"}}'


def frame(header: JsonObject, entries: Iterable[Entry], blob: Callable[[str], Iterable[bytes]]) -> Iterator[bytes]:
    """The whole Bundle: `header` (every field but `entry`), then the entries, comma-separated."""
    if "entry" in header:
        raise ValueError("the header must not carry entries")
    head = dumps(header)
    yield (head[:-1] + ',"entry":[').encode()
    for n, entry in enumerate(entries):
        if n:
            yield b","
        yield from entry_bytes(entry, blob)
    yield b"]}"


class Digest:
    """Passes chunks through, counting bytes and hashing them. Read `sha256` and `size` after the
    stream is exhausted."""

    def __init__(self, chunks: Iterable[bytes]) -> None:
        self._chunks = chunks
        self._hash = hashlib.sha256()
        self.size = 0

    def __iter__(self) -> Iterator[bytes]:
        for chunk in self._chunks:
            self._hash.update(chunk)
            self.size += len(chunk)
            yield chunk

    @property
    def sha256(self) -> str:
        return self._hash.hexdigest()
