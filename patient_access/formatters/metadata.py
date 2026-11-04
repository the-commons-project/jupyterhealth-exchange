"""Reading a delivered bundle's provenance back: reference code for consumers, and what the tests
use to check what the formatter wrote."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from patient_access.core.models import DocumentResult, JsonObject, OutcomeStatus, SkipReason, Value

from .document_bundle import DOCUMENTS_OUTCOME_EXTENSION, PULL_EXTENSION, SECTION_OUTCOME_EXTENSION


class SectionOutcome(Value):
    resource_type: str
    label: str
    status: OutcomeStatus
    count: int
    error: str | None = None


class DocumentEntry(Value):
    referenced_by: str
    result: DocumentResult
    content_type: str | None = None
    reason: SkipReason | None = None
    size: int | None = None
    sha256: str | None = None


class PullMetadata(Value):
    job_id: str
    participant_ref: str
    participant_attrs: dict[str, str | int | bool | float | list[str | int]]
    source_brand: str
    scopes_requested: tuple[str, ...]
    scopes_granted: tuple[str, ...]
    scopes_declined: tuple[str, ...]
    outcomes: tuple[SectionOutcome, ...]
    documents: tuple[DocumentEntry, ...]
    meta_source: str | None = None


_VALUE_KEYS = ("valueString", "valueInteger", "valueBoolean", "valueDecimal", "valueDateTime")


def _value(e: JsonObject) -> Any:
    return next((e[k] for k in _VALUE_KEYS if k in e), None)


def _children(e: JsonObject) -> list[JsonObject]:
    children = e.get("extension")
    return children if isinstance(children, list) else []


def _one(children: list[JsonObject], url: str) -> JsonObject:
    return next((c for c in children if c.get("url") == url), {})


def _values(children: list[JsonObject], url: str) -> tuple[Any, ...]:
    return tuple(_value(c) for c in children if c.get("url") == url)


def _first(children: list[JsonObject], url: str) -> Any:
    found = _values(children, url)
    return found[0] if found else None


def _ending(extensions: Any, suffix: str) -> Iterator[JsonObject]:
    for e in extensions if isinstance(extensions, list) else []:
        if isinstance(e, dict) and str(e.get("url", "")).endswith(suffix):
            yield e


def pull_metadata(bundle: JsonObject) -> PullMetadata:
    composition = bundle["entry"][0]["resource"]
    pull = next(_ending(composition.get("extension"), "/" + PULL_EXTENSION))
    parts = _children(pull)
    participant = _children(_one(parts, "participant"))
    attrs: dict[str, Any] = {}
    for c in participant:
        if c.get("url") in ("ref", "provider"):
            continue
        name, value = str(c["url"]), _value(c)
        attrs[name] = [*attrs[name], value] if isinstance(attrs.get(name), list) else value
    scopes = _children(_one(parts, "scopes"))
    outcomes: list[SectionOutcome] = []
    documents: list[DocumentEntry] = []
    for section in composition.get("section") or []:
        for e in _ending(section.get("extension"), "/" + SECTION_OUTCOME_EXTENSION):
            oc = _children(e)
            outcomes.append(
                SectionOutcome(
                    resource_type=section["code"]["text"],
                    label=_first(oc, "label"),
                    status=OutcomeStatus(_first(oc, "status")),
                    count=_first(oc, "count"),
                    error=_first(oc, "error"),
                )
            )
        for e in _ending(section.get("extension"), "/" + DOCUMENTS_OUTCOME_EXTENSION):
            for d in (c for c in _children(e) if c.get("url") == "document"):
                dc = _children(d)
                reason = _first(dc, "reason")
                documents.append(
                    DocumentEntry(
                        referenced_by=_first(dc, "referencedBy"),
                        result=DocumentResult(_first(dc, "result")),
                        content_type=_first(dc, "contentType"),
                        reason=SkipReason(reason) if reason else None,
                        size=_first(dc, "size"),
                        sha256=_first(dc, "sha256"),
                    )
                )
    return PullMetadata(
        job_id=_first(_children(_one(parts, "job")), "id"),
        participant_ref=_first(participant, "ref"),
        participant_attrs=attrs,
        source_brand=_first(_children(_one(parts, "source")), "brand"),
        scopes_requested=_values(scopes, "requested"),
        scopes_granted=_values(scopes, "granted"),
        scopes_declined=_values(scopes, "declined"),
        outcomes=tuple(outcomes),
        documents=tuple(documents),
        meta_source=(composition.get("meta") or {}).get("source"),
    )
