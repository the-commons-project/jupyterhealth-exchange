"""DocumentBundleFormatter: one FHIR R4 `document` Bundle per pull, as a header and an entry
iterator, with everything a consumer needs inside it.

    Composition       first; one section per resource type; the pull's provenance and every
                      fetch outcome as extensions; `meta.source` ties it back to the host
    Device            the author: this software
    the resources     `urn:uuid` fullUrls, internal references rewritten to them, references to
                      the same server's resources not in the pull made absolute against it
    Binary            one per stored document, last; its bytes base64-streamed by the framer.
                      The attachment that referenced the document points at it by fullUrl, as
                      an EHR's DocumentReference points at its own Binary. The Composition's
                      "Document content" section lists them, so every entry is reachable from
                      the Composition, as a document Bundle requires.

An attachment the pass tried and could not deliver (refused by the caps, failed to fetch,
stored but corrupt) loses any inline `data`, so the EHR's inline bytes never slip past the caps;
it keeps its `url`, `size` and `hash`. An attachment nobody tried (an alternate format whose
sibling was stored) stays as the EHR sent it. Every url is made absolute.

Two passes over the spool: the first collects ids (to rewrite references and list sections), the
second emits entries. Nothing holds more than one resource at a time beyond the id index.
Deterministic: the same spool and job give the same bytes (except the bundle's timestamp).

Known limitation, shared with fhir.resources and the browser's JSON parser: a decimal's textual
precision is not kept (`1.10` is written `1.10` only if the EHR's JSON parsed to that float; it
comes out `1.1`). The value is never wrong.
"""

from __future__ import annotations

import html
import uuid
from collections.abc import Container, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from patient_access import __version__
from patient_access.core.documents.candidates import blob_id, resource_key
from patient_access.core.interfaces import SpoolHandle
from patient_access.core.models import (
    A,
    BlobMeta,
    DocumentOutcome,
    DocumentResult,
    DocumentSource,
    Job,
    JsonObject,
    OutcomeStatus,
    SpoolIndex,
    TypeOutcome,
)
from patient_access.core.scopes import ScopeSet
from patient_access.core.settings import BundleSettings

from .stream import BinaryEntry, Entry, ResourceEntry

NS = uuid.UUID("6f1c7d0e-3b0a-4c5e-9a57-1d2c0b7e9a10")
SOFTWARE = "patient_access"
LOINC_SUMMARY = {"system": "http://loinc.org", "code": "60591-5", "display": "Patient summary Document"}
EMPTY_REASON = "http://terminology.hl7.org/CodeSystem/list-empty-reason"


class EmptyReason(StrEnum):
    """Why a section has no entries (HL7 list-empty-reason)."""

    UNAVAILABLE = "unavailable"  # a fetch for it failed
    WITHHELD = "withheld"  # not selected by the patient, or not granted
    NILKNOWN = "nilknown"  # fetched, and there was nothing


PULL_EXTENSION = "patient-access-pull"
SECTION_OUTCOME_EXTENSION = "patient-access-section-outcome"
DOCUMENTS_OUTCOME_EXTENSION = "patient-access-documents-outcome"
DOCUMENTS_SECTION_CODE = "documents"

SECTION_TITLES: Mapping[str, str] = {
    "Patient": "Demographics",
    "AllergyIntolerance": "Allergies and intolerances",
    "Condition": "Problems",
    "MedicationRequest": "Medications",
    "MedicationDispense": "Medication dispenses",
    "Immunization": "Immunizations",
    "Procedure": "Procedures",
    "Observation": "Results and vital signs",
    "DiagnosticReport": "Diagnostic reports",
    "DocumentReference": "Clinical notes",
    "Encounter": "Encounters",
    "CarePlan": "Care plans",
    "CareTeam": "Care teams",
    "Goal": "Goals",
    "ServiceRequest": "Service requests",
    "Device": "Devices",
    "QuestionnaireResponse": "Questionnaire responses",
}

ExtensionValue = str | int | float | bool | datetime | None


def urn(resource_type: str, key: str) -> str:
    return f"urn:uuid:{uuid.uuid5(NS, f'{resource_type}/{key}')}"


# --- extensions ------------------------------------------------------------------------------


def ext(url: str, value: ExtensionValue) -> JsonObject | None:
    """A simple extension, or None for a missing value (dropped by `nested`)."""
    match value:
        case None:
            return None
        case bool():
            return {"url": url, "valueBoolean": value}
        case int():
            return {"url": url, "valueInteger": value}
        case float():
            return {"url": url, "valueDecimal": value}
        case datetime():
            return {"url": url, "valueDateTime": value.isoformat()}
        case str():
            return {"url": url, "valueString": value}


def repeated(url: str, values: Iterable[ExtensionValue]) -> list[JsonObject | None]:
    return [ext(url, v) for v in values]


def nested(url: str, children: list[JsonObject | None]) -> JsonObject:
    return {"url": url, "extension": [c for c in children if c is not None]}


def attrs_extensions(attrs: Mapping[str, Any]) -> list[JsonObject | None]:
    out: list[JsonObject | None] = []
    for name, value in attrs.items():
        if isinstance(value, list | tuple):
            out += repeated(name, value)
        elif isinstance(value, ExtensionValue):
            out.append(ext(name, value))
        else:
            out.append(ext(name, str(value)))
    return out


def pull_extension(job: Job[A], settings: BundleSettings, now: datetime) -> JsonObject:
    src = job.source
    not_granted = ScopeSet.parse(src.scopes_granted).withheld(src.scopes_requested)
    host = [ext("hostName", settings.host_name), ext("hostVersion", settings.host_version)]
    return nested(
        settings.extension_base_url + PULL_EXTENSION,
        [
            # `finished`: when the bundle was assembled, the end of the pull it records.
            nested(
                "job",
                [ext("id", job.id), ext("created", job.created_at), ext("finished", now), ext("runs", job.runs)],
            ),
            nested(
                "participant",
                [
                    ext("ref", job.participant.ref),
                    ext("provider", job.participant.provider),
                    *attrs_extensions(job.participant.attrs.model_dump(mode="json")),
                ],
            ),
            nested(
                "source",
                [
                    ext("id", src.id),
                    ext("vendor", src.vendor),
                    ext("brand", src.brand_name),
                    ext("brandId", src.brand_id),
                    ext("brandNpi", src.brand_npi),
                    ext("facility", src.facility_name),
                    ext("facilityId", src.facility_id),
                    ext("iss", src.iss),
                    ext("endpointId", src.endpoint_id),
                    ext("customerId", src.customer_id),
                    ext("customerName", src.customer_name),
                    ext("ehrPatientId", src.ehr_patient_id),
                    ext("authMethod", src.auth_method.value),
                    ext("pkceAdvertised", src.pkce_advertised),
                    # Who chose the record types: our checklist ("host"), or the EHR's own screen
                    # ("ehr"), in which case notGranted includes types the patient unticked there.
                    ext("typeChoice", src.type_choice.value),
                ],
            ),
            nested(
                "scopes",
                [
                    *repeated("requested", src.scopes_requested),
                    *repeated("granted", src.scopes_granted),
                    *repeated("declined", src.scopes_declined),
                    *repeated("notGranted", not_granted),
                ],
            ),
            nested("software", [ext("name", SOFTWARE), ext("version", __version__), *host]),
        ],
    )


def outcome_extension(o: TypeOutcome, base: str) -> JsonObject:
    return nested(
        base + SECTION_OUTCOME_EXTENSION,
        [
            ext("label", o.label),
            ext("query", o.query),
            ext("status", o.status.value),
            ext("count", o.count),
            ext("pages", o.pages),
            ext("error", o.error),
            ext("capped", o.capped),
            ext("seconds", o.seconds),
        ],
    )


def documents_extension(docs: DocumentOutcome, binaries: Mapping[str, str], base: str) -> JsonObject:
    """`binaries`: blob id -> the Binary entry's fullUrl, for the documents in the bundle."""
    return nested(
        base + DOCUMENTS_OUTCOME_EXTENSION,
        [
            ext("status", docs.status.value),
            ext("error", docs.error),
            ext("stored", len(binaries)),
            # A decimal, not an integer: R4's valueInteger is 32-bit and a pull's documents may
            # total more than 2 GiB (each one is capped below that).
            ext("bytes", float(sum(r.size or 0 for r in docs.stored if r.blob_id in binaries))),
            ext("seconds", docs.seconds),
            ext("capped", docs.capped),
            *[
                nested("skipped", [ext("reason", reason.value), ext("count", count)])
                for reason, count in sorted(docs.skipped.items())
            ],
            *[
                nested(
                    "document",
                    [
                        ext("referencedBy", r.referenced_by),
                        ext("source", r.source.value),
                        ext("attachment", r.attachment_index),
                        ext("contentType", r.content_type),
                        ext("result", r.result.value),
                        ext("reason", r.reason.value if r.reason else None),
                        ext("detail", r.detail),
                        ext("size", r.size),
                        ext("sha256", r.sha256),
                        ext("binary", binaries.get(r.blob_id)),
                    ],
                )
                for r in docs.records
            ],
        ],
    )


# --- references ------------------------------------------------------------------------------


def _reference_key(reference: str, iss: str) -> str | None:
    """`Type/id` for a reference to a resource on this server (relative, or absolute under its
    base URL; a version suffix `/_history/n` dropped), else None (a reference to another server
    is never mistaken for one of ours, however its path ends)."""
    if reference.startswith(iss + "/"):
        rel = reference[len(iss) + 1 :]
    elif "://" in reference:
        return None
    else:
        rel = reference.lstrip("/")
    parts = rel.split("/")
    if len(parts) == 4 and parts[2] == "_history":
        parts = parts[:2]
    return "/".join(parts) if len(parts) == 2 and all(parts) else None


def rewrite_refs(node: Any, index: Mapping[str, str], iss: str) -> Any:
    """Every `reference` to a resource in the bundle becomes its urn; other references to this
    server are made absolute so they stay meaningful outside it; references elsewhere are kept."""
    if isinstance(node, dict):
        out: JsonObject = {}
        for k, v in node.items():
            if k == "reference" and isinstance(v, str) and not v.startswith(("#", "urn:")):
                key = _reference_key(v, iss)
                out[k] = (index.get(key) if key else None) or (v if "://" in v else f"{iss}/{v.lstrip('/')}")
            else:
                out[k] = rewrite_refs(v, index, iss)
        return out
    if isinstance(node, list):
        return [rewrite_refs(v, index, iss) for v in node]
    return node


# --- the bundle ------------------------------------------------------------------------------


@dataclass
class _Index:
    """Pass 1: every resource's urn and each type's section entries. A mutable dataclass, not a
    pydantic model: an accumulator filled one resource at a time across the whole spool, which
    validated assignment would copy as it grows."""

    urns: dict[str, str] = field(default_factory=dict)  # "Type/key" -> urn
    sections: dict[str, list[str]] = field(default_factory=dict)  # type -> urns


def resource_types(job: Job[A], index: SpoolIndex) -> list[str]:
    """Types in query-table order, then anything else the spool holds, sorted."""
    ordered = list(dict.fromkeys(o.resource_type for o in job.outcomes))
    return ordered + sorted(t for t in index.types if t not in ordered)


def _collect(spool: SpoolHandle, types: list[str]) -> _Index:
    idx = _Index()
    for t in types:
        for position, r in enumerate(spool.iter_type(t)):
            key = resource_key(r, position)
            u = urn(t, key)
            idx.urns[f"{t}/{key}"] = u
            idx.sections.setdefault(t, []).append(u)
    return idx


def _empty_reason(outcomes: list[TypeOutcome]) -> EmptyReason:
    if any(o.status is OutcomeStatus.ERROR for o in outcomes):
        return EmptyReason.UNAVAILABLE
    if outcomes and all(o.status.withheld or o.status is OutcomeStatus.SKIPPED for o in outcomes):
        return EmptyReason.WITHHELD  # not requested, not granted, or the scope not granted at all
    return EmptyReason.NILKNOWN


def _empty(reason: EmptyReason) -> JsonObject:
    return {"coding": [{"system": EMPTY_REASON, "code": reason.value}]}


GENERATED_BY = "This statement was generated by {host}; it is not a record from the healthcare organization."
_WHY = {
    EmptyReason.UNAVAILABLE: "the healthcare organization's server did not return them",
    EmptyReason.WITHHELD: "they were not requested by the patient, or not granted",
    EmptyReason.NILKNOWN: "the healthcare organization returned none",
}


def _generated_text(title: str, reason: EmptyReason, host: str, why: str | None = None) -> JsonObject:
    """The narrative an empty section must carry (R4 cmp-1: text, entries or sub-sections). It is
    the module's own words, and says so, so no reader takes it for the EHR's."""
    sentence = f"{title}: no records are included in this document ({why or _WHY[reason]}). " + GENERATED_BY.format(
        host=host
    )
    return {"status": "generated", "div": f'<div xmlns="http://www.w3.org/1999/xhtml">{html.escape(sentence)}</div>'}


def _section(t: str, urns: list[str], outcomes: list[TypeOutcome], base: str, host: str) -> JsonObject:
    title = SECTION_TITLES.get(t, t)
    section: JsonObject = {"title": title, "code": {"text": t}}
    if outcomes:
        section["extension"] = [outcome_extension(o, base) for o in outcomes]
    if urns:
        section["entry"] = [{"reference": u} for u in urns]
    else:
        reason = _empty_reason(outcomes)
        section["text"] = _generated_text(title, reason, host)
        section["emptyReason"] = _empty(reason)
    return section


def _documents_section(job: Job[A], binaries: Mapping[str, str], base: str, host: str) -> JsonObject:
    title = "Document content"
    section: JsonObject = {
        "title": title,
        "code": {"text": DOCUMENTS_SECTION_CODE},
        "extension": [documents_extension(job.documents, binaries, base)],
    }
    if binaries:
        section["entry"] = [{"reference": u} for u in binaries.values()]
    else:
        tried = len(job.documents.records)
        if tried:
            reason = EmptyReason.UNAVAILABLE
            why = f"{tried} documents were offered and none could be included; see the documents outcome"
        else:
            reason = EmptyReason.UNAVAILABLE if job.documents.status is OutcomeStatus.ERROR else EmptyReason.NILKNOWN
            why = _WHY[reason]
        section["text"] = _generated_text(title, reason, host, why)
        section["emptyReason"] = _empty(reason)
    return section


def _absolute(url: str, iss: str) -> str:
    return url if "://" in url else f"{iss}/{url.lstrip('/')}"


def binary_urn(blob_id: str) -> str:
    return urn("Binary", blob_id)


def _point_attachments(
    resource: JsonObject, t: str, key: str, delivered: Mapping[str, BlobMeta], tried: Container[str], iss: str
) -> None:
    """Every attachment of a document-bearing resource: the ones with a document in the bundle
    point at its Binary; the ones the pass tried and could not deliver (refused by the caps,
    failed to fetch, stored but corrupt) lose any inline `data`, so the caps bound the bundle;
    the rest, which nobody tried, stay as the EHR sent them. Every url is made absolute."""
    source = {s.resource_type: s for s in DocumentSource}.get(t)
    if source is None:
        return
    items = resource.get("content" if source is DocumentSource.DOCUMENT_REFERENCE else "presentedForm")
    if not isinstance(items, list):
        return
    for i, item in enumerate(items):
        att = item.get("attachment") if source is DocumentSource.DOCUMENT_REFERENCE and isinstance(item, dict) else item
        if not isinstance(att, dict):
            continue
        bid = blob_id(t, key, i)
        meta = delivered.get(bid)
        if meta is not None:
            att.pop("data", None)
            att["url"] = binary_urn(bid)
            att["contentType"] = meta.content_type
            att["size"] = meta.size
            continue
        if bid in tried:
            att.pop("data", None)
        if isinstance(att.get("url"), str):
            att["url"] = _absolute(att["url"], iss)


@runtime_checkable
class HasSourceUri(Protocol):
    """Host attributes that name the host's record of the pull; it becomes `meta.source`."""

    fhir_source_uri: str


def document_bundle(
    job: Job[A], spool: SpoolHandle, settings: BundleSettings, now: datetime
) -> tuple[JsonObject, Iterator[Entry]]:
    """The Bundle's header (every field but `entry`) and its entries, Composition first."""
    base = settings.extension_base_url
    host = settings.host_name or SOFTWARE  # who wrote the generated narratives
    iss = job.source.iss.rstrip("/")
    types = resource_types(job, spool.index())
    idx = _collect(spool, types)
    delivered: dict[str, BlobMeta] = {}
    for r in job.documents.records:
        if r.result is DocumentResult.STORED and (meta := spool.blob_meta(r.blob_id)) is not None:
            delivered[r.blob_id] = meta
    binaries = {bid: binary_urn(bid) for bid in delivered}
    tried = frozenset(r.blob_id for r in job.documents.records)

    by_type: dict[str, list[TypeOutcome]] = {}
    for o in job.outcomes:
        by_type.setdefault(o.resource_type, []).append(o)

    device_id = str(uuid.uuid5(NS, f"author/{SOFTWARE}"))
    composition_id = str(uuid.uuid5(NS, f"composition/{job.id}"))
    patient_key = f"Patient/{job.source.ehr_patient_id}"
    composition: JsonObject = {
        "resourceType": "Composition",
        "id": composition_id,
        "extension": [pull_extension(job, settings, now)],
        "status": "final",
        "type": {"coding": [LOINC_SUMMARY]},
        "subject": {"reference": idx.urns.get(patient_key, f"{iss}/{patient_key}")},
        "date": now.isoformat(),
        "author": [{"reference": f"urn:uuid:{device_id}"}],
        "title": f"Records shared from {job.source.brand_name}",
        "section": [
            *[_section(t, idx.sections.get(t, []), by_type.get(t, []), base, host) for t in types],
            _documents_section(job, binaries, base, host),
        ],
    }
    attrs: object = job.participant.attrs  # the host's model: may or may not name a source URI
    if isinstance(attrs, HasSourceUri) and attrs.fhir_source_uri:
        composition["meta"] = {"source": attrs.fhir_source_uri}
    device: JsonObject = {
        "resourceType": "Device",
        "id": device_id,
        "deviceName": [{"name": SOFTWARE, "type": "user-friendly-name"}],
        "version": [{"value": __version__}],
    }
    header: JsonObject = {
        "resourceType": "Bundle",
        "id": str(uuid.uuid5(NS, f"bundle/{job.id}")),
        "identifier": {"system": "urn:ietf:rfc:3986", "value": f"urn:uuid:{uuid.uuid5(NS, f'bundle/{job.id}')}"},
        "type": "document",
        "timestamp": now.isoformat(),
    }

    def entries() -> Iterator[Entry]:
        yield ResourceEntry(full_url=f"urn:uuid:{composition_id}", resource=composition)
        yield ResourceEntry(full_url=f"urn:uuid:{device_id}", resource=device)
        for t in types:
            for position, raw in enumerate(spool.iter_type(t)):
                key = resource_key(raw, position)
                resource = rewrite_refs(raw, idx.urns, iss)
                _point_attachments(resource, t, key, delivered, tried, iss)
                yield ResourceEntry(full_url=idx.urns[f"{t}/{key}"], resource=resource)
        for bid, meta in delivered.items():
            yield BinaryEntry(
                full_url=binaries[bid], id=bid, content_type=meta.content_type, blob_id=bid, size=meta.size
            )

    return header, entries()
