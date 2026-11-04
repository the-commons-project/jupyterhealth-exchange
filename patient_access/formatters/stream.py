"""Bytes out of entries, in flat memory: bundle framing, documents base64-encoded in flight, and a
digest of everything that passes. All pure: iterators in, iterators out.

A document's base64 is streamed into its resource's JSON without ever building the string: the
entry is serialised with a unique placeholder where `attachment.data` goes, the text is split at
the placeholder, and the blob is encoded between the two halves. Chunks are encoded in multiples
of 3 bytes, so no padding appears mid-string, and the base64 alphabet needs no JSON escaping.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import Any

from patient_access.core.models import JsonObject


class FramingError(Exception):
    """An entry's placeholder was not found in its own serialisation (a bug, not bad data)."""


@dataclass(frozen=True, slots=True)
class InlineBlob:
    """Where in an entry a blob's base64 goes. A frozen dataclass, not a pydantic model: built
    once per document in the formatter's hot loop, nothing to validate."""

    placeholder: str
    blob_id: str


@dataclass(frozen=True, slots=True)
class Entry:
    """One Bundle entry. `resource` is the FHIR resource as JSON (references rewritten,
    placeholders in place of inline data). A dataclass for the same reason as InlineBlob: a
    resource can be large and must not be copied by validation."""

    full_url: str
    resource: JsonObject
    inline: tuple[InlineBlob, ...] = ()


def dumps(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


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
    text = dumps({"fullUrl": entry.full_url, "resource": entry.resource})
    if not entry.inline:
        yield text.encode()
        return
    located = sorted(((text.find(dumps(b.placeholder)), b) for b in entry.inline), key=lambda pair: pair[0])
    cursor = 0
    for position, b in located:
        if position < cursor:
            raise FramingError(f"placeholder for {b.blob_id} not found")
        yield (text[cursor:position] + '"').encode()
        yield from base64_stream(blob(b.blob_id))
        yield b'"'
        cursor = position + len(dumps(b.placeholder))
    yield text[cursor:].encode()


def frame(header: JsonObject, entries: Iterable[Entry], blob: Callable[[str], Iterable[bytes]]) -> Iterator[bytes]:
    """The whole Bundle: `header` (every field but `entry`), then the entries, comma-separated."""
    if "entry" in header:
        raise FramingError("the header must not carry entries")
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
