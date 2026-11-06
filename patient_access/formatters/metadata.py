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
    binary: str | None = None  # the fullUrl of the document's Binary entry, when it is in the bundle


AttrValue = str | int | bool | float | list[str | int]


class NotAPullBundle(ValueError):
    """The object is not a bundle this library wrote: no Composition with the pull extension."""


class PullMetadata(Value):
    job_id: str
    finished: str | None = None
    participant_ref: str
    participant_attrs: dict[str, AttrValue]
    source_brand: str
    type_choice: str | None = None
    scopes_requested: tuple[str, ...]
    scopes_granted: tuple[str, ...]
    scopes_declined: tuple[str, ...]
    outcomes: tuple[SectionOutcome, ...]
    documents: tuple[DocumentEntry, ...]
    meta_source: str | None = None


_VALUE_KEYS = ("valueString", "valueInteger", "valueBoolean", "valueDecimal", "valueDateTime")


Scalar = str | int | bool | float | None


def _value(e: JsonObject) -> Scalar:
    for k in _VALUE_KEYS:
        if k in e:
            v = e[k]
            return v if isinstance(v, str | int | bool | float) else None
    return None


def _children(e: JsonObject) -> list[JsonObject]:
    children = e.get("extension")
    return [c for c in children if isinstance(c, dict)] if isinstance(children, list) else []


def _one(children: list[JsonObject], url: str) -> JsonObject:
    return next((c for c in children if c.get("url") == url), {})


def _values(children: list[JsonObject], url: str) -> tuple[Scalar, ...]:
    return tuple(_value(c) for c in children if c.get("url") == url)


def _strings(children: list[JsonObject], url: str) -> tuple[str, ...]:
    return tuple(v for v in _values(children, url) if isinstance(v, str))


def _text(children: list[JsonObject], url: str) -> str | None:
    found = _strings(children, url)
    return found[0] if found else None


def _int(children: list[JsonObject], url: str) -> int | None:
    found = [v for v in _values(children, url) if isinstance(v, int) and not isinstance(v, bool)]
    return found[0] if found else None


def _ending(extensions: Any, suffix: str) -> Iterator[JsonObject]:
    for e in extensions if isinstance(extensions, list) else []:
        if isinstance(e, dict) and str(e.get("url", "")).endswith(suffix):
            yield e


def _composition(bundle: JsonObject) -> JsonObject:
    entries = bundle.get("entry")
    first = entries[0] if isinstance(entries, list) and entries and isinstance(entries[0], dict) else {}
    resource = first.get("resource")
    if not (isinstance(resource, dict) and resource.get("resourceType") == "Composition"):
        raise NotAPullBundle("the first entry is not a Composition")
    return resource


def pull_metadata(bundle: JsonObject) -> PullMetadata:
    composition = _composition(bundle)
    pull = next(_ending(composition.get("extension"), "/" + PULL_EXTENSION), None)
    if pull is None:
        raise NotAPullBundle("no Composition carrying the patient-access-pull extension")
    parts = _children(pull)
    participant = _children(_one(parts, "participant"))
    attrs: dict[str, AttrValue] = {}
    for c in participant:
        name, value = str(c.get("url")), _value(c)
        if name in ("ref", "provider") or value is None:
            continue
        held = attrs.get(name)
        if held is None or not isinstance(value, str | int):
            attrs[name] = value
        elif isinstance(held, list):
            attrs[name] = [*held, value]
        elif isinstance(held, str | int):
            attrs[name] = [held, value]  # a repeated extension is a list
    scopes = _children(_one(parts, "scopes"))
    outcomes: list[SectionOutcome] = []
    documents: list[DocumentEntry] = []
    for section in composition.get("section") or []:
        for e in _ending(section.get("extension"), "/" + SECTION_OUTCOME_EXTENSION):
            oc = _children(e)
            outcomes.append(
                SectionOutcome(
                    resource_type=str((section.get("code") or {}).get("text")),
                    label=_text(oc, "label") or "",
                    status=OutcomeStatus(_text(oc, "status") or ""),
                    count=_int(oc, "count") or 0,
                    error=_text(oc, "error"),
                )
            )
        for e in _ending(section.get("extension"), "/" + DOCUMENTS_OUTCOME_EXTENSION):
            for d in (c for c in _children(e) if c.get("url") == "document"):
                dc = _children(d)
                reason = _text(dc, "reason")
                documents.append(
                    DocumentEntry(
                        referenced_by=_text(dc, "referencedBy") or "",
                        result=DocumentResult(_text(dc, "result") or ""),
                        content_type=_text(dc, "contentType"),
                        reason=SkipReason(reason) if reason else None,
                        size=_int(dc, "size"),
                        sha256=_text(dc, "sha256"),
                        binary=_text(dc, "binary"),
                    )
                )
    job = _children(_one(parts, "job"))
    source = _children(_one(parts, "source"))
    return PullMetadata(
        job_id=_text(job, "id") or "",
        finished=_text(job, "finished"),
        participant_ref=_text(participant, "ref") or "",
        participant_attrs=attrs,
        source_brand=_text(source, "brand") or "",
        type_choice=_text(source, "typeChoice"),
        scopes_requested=_strings(scopes, "requested"),
        scopes_granted=_strings(scopes, "granted"),
        scopes_declined=_strings(scopes, "declined"),
        outcomes=tuple(outcomes),
        documents=tuple(documents),
        meta_source=(composition.get("meta") or {}).get("source"),
    )
