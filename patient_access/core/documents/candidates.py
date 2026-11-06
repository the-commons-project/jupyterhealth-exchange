"""Where the documents are: DocumentReference content and DiagnosticReport renderings.

A DocumentReference often offers one document in several formats; one is taken, by the vendor's
preference order. A DiagnosticReport's `presentedForm` items are distinct renderings; each is
taken. Blob ids are derived from the resource, so the formatter finds the same blob the
downloader stored without any table between them: the type, a hash of the resource's id and the
attachment's index. A hash, not the id cleaned up: two ids that clean to the same text (`a:b`,
`a;b`, or long ids sharing a prefix) would collide, and the id would sit in a file name.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator, Sequence
from typing import Any

from ..interfaces import SpoolHandle
from ..models import DocumentSource, JsonObject, Value
from ..source_settings import DocumentProfile

BLOB_HASH_CHARS = 24  # of SHA-256 hex: 96 bits, no collision among a pull's documents


def resource_key(resource: JsonObject, position: int) -> str:
    """A resource's id, or its position in the spool when the server sent none."""
    rid = resource.get("id")
    return rid if isinstance(rid, str) and rid else f"pos{position}"


def blob_id(resource_type: str, key: str, attachment_index: int) -> str:
    """Path-safe, fixed length, and a valid FHIR id (the Binary entry's)."""
    digest = hashlib.sha256(key.encode()).hexdigest()[:BLOB_HASH_CHARS]
    return f"{resource_type}-{digest}-{attachment_index}"


def media_type(content_type: str | None) -> str:
    """`text/html; charset=utf-8` -> `text/html`."""
    return (content_type or "").split(";")[0].strip().lower()


def preferred(content_types: Sequence[str | None], prefer: Sequence[str]) -> int | None:
    """The index of the item to take: the first in `prefer` order, earliest on ties, unknown
    types last. None for no items."""
    if not content_types:
        return None

    def rank(i: int) -> tuple[int, int]:
        mt = media_type(content_types[i])
        return (prefer.index(mt) if mt in prefer else len(prefer), i)

    return min(range(len(content_types)), key=rank)


class Candidate(Value):
    blob_id: str
    source: DocumentSource
    referenced_by: str
    attachment_index: int
    content_type: str | None
    url: str | None = None
    data: str | None = None  # inline base64


def _attachment(item: Any, source: DocumentSource) -> JsonObject:
    if not isinstance(item, dict):
        return {}
    if source is DocumentSource.DOCUMENT_REFERENCE:
        att = item.get("attachment")
        return att if isinstance(att, dict) else {}
    return item


def _candidate(resource: JsonObject, key: str, source: DocumentSource, index: int, att: JsonObject) -> Candidate:
    rtype = source.resource_type
    url, data, ctype = att.get("url"), att.get("data"), att.get("contentType")
    return Candidate(
        blob_id=blob_id(rtype, key, index),
        source=source,
        referenced_by=f"{rtype}/{key}",
        attachment_index=index,
        content_type=ctype if isinstance(ctype, str) else None,
        url=url if isinstance(url, str) and url else None,
        data=data if isinstance(data, str) and data else None,
    )


def chosen_indexes(resource: JsonObject, source: DocumentSource, prefer: Sequence[str]) -> list[int]:
    """The attachment indexes of a resource that are candidates. Shared with the formatter."""
    field = "content" if source is DocumentSource.DOCUMENT_REFERENCE else "presentedForm"
    items = resource.get(field)
    if not isinstance(items, list) or not items:
        return []
    if source is DocumentSource.DOCUMENT_REFERENCE:
        types = [_attachment(item, source).get("contentType") for item in items]
        best = preferred([t if isinstance(t, str) else None for t in types], prefer)
        return [] if best is None else [best]
    return list(range(len(items)))


def attachments(resource: JsonObject, source: DocumentSource) -> list[JsonObject]:
    field = "content" if source is DocumentSource.DOCUMENT_REFERENCE else "presentedForm"
    items = resource.get(field)
    return [_attachment(item, source) for item in items] if isinstance(items, list) else []


def candidates(spool: SpoolHandle, profile: DocumentProfile) -> Iterator[Candidate]:
    for source in sorted(profile.sources):
        for position, resource in enumerate(spool.iter_type(source.resource_type)):
            key = resource_key(resource, position)
            atts = attachments(resource, source)
            for i in chosen_indexes(resource, source, profile.prefer):
                yield _candidate(resource, key, source, i, atts[i])
