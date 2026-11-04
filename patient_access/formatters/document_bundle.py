"""DocumentBundleFormatter: one FHIR R4 `document` Bundle per pull, as a header and an entry
iterator, with everything a consumer needs inside it.

    Composition       first; one section per resource type; the pull's provenance and every
                      fetch outcome as extensions; `meta.source` ties it back to the host
    Device            the author: this software
    the resources     `urn:uuid` fullUrls, internal references rewritten to them, references to
                      things not in the pull made absolute against the server
    documents         inline as base64 in `attachment.data`, streamed by the framer

Two passes over the spool: the first collects ids (to rewrite references and list sections), the
second emits entries. Nothing holds more than one resource at a time beyond the id index.
Deterministic: the same spool and job give the same bytes (except the bundle's timestamp).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from patient_access import __version__
from patient_access.core.documents.candidates import blob_id, resource_key
from patient_access.core.interfaces import SpoolHandle
from patient_access.core.models import (
    A,
    DocumentOutcome,
    DocumentSource,
    Job,
    JsonObject,
    OutcomeStatus,
    SpoolIndex,
    TypeOutcome,
)
from patient_access.core.settings import BundleSettings

from .stream import Entry, InlineBlob

NS = uuid.UUID("6f1c7d0e-3b0a-4c5e-9a57-1d2c0b7e9a10")
SOFTWARE = "patient_access"
LOINC_SUMMARY = {"system": "http://loinc.org", "code": "60591-5", "display": "Patient summary Document"}
EMPTY_REASON = "http://terminology.hl7.org/CodeSystem/list-empty-reason"

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


def repeated(url: str, values: Any) -> list[JsonObject | None]:
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


def pull_extension(job: Job[A], base: str) -> JsonObject:
    src = job.source
    not_granted = sorted({s for s in src.scopes_requested if s.startswith("patient/")} - set(src.scopes_granted))
    return nested(
        base + PULL_EXTENSION,
        [
            nested("job", [ext("id", job.id), ext("created", job.created_at), ext("runs", job.runs)]),
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
                    ext("authMethod", src.auth_method),
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
            nested("software", [ext("name", SOFTWARE), ext("version", __version__)]),
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


def documents_extension(docs: DocumentOutcome, base: str) -> JsonObject:
    return nested(
        base + DOCUMENTS_OUTCOME_EXTENSION,
        [
            ext("status", docs.status.value),
            ext("error", docs.error),
            ext("stored", len(docs.stored)),
            ext("bytes", docs.bytes),
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
                    ],
                )
                for r in docs.records
            ],
        ],
    )


# --- references ------------------------------------------------------------------------------


def rewrite_refs(node: Any, index: Mapping[str, str], iss: str) -> Any:
    """Every `reference` naming a resource in the bundle becomes its urn; the rest are made
    absolute against the server so they stay meaningful outside it."""
    if isinstance(node, dict):
        out: JsonObject = {}
        for k, v in node.items():
            if k == "reference" and isinstance(v, str) and not v.startswith(("#", "urn:")):
                rel = v[len(iss) + 1 :] if v.startswith(iss + "/") else v
                parts = rel.split("/")
                key = "/".join(parts[-2:]) if len(parts) >= 2 else rel
                out[k] = index.get(key) or (v if "://" in v else f"{iss}/{rel}")
            else:
                out[k] = rewrite_refs(v, index, iss)
        return out
    if isinstance(node, list):
        return [rewrite_refs(v, index, iss) for v in node]
    return node


# --- the bundle ------------------------------------------------------------------------------


@dataclass
class _Index:
    """Pass 1: every resource's urn and each type's section entries."""

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


def _empty_reason(outcomes: list[TypeOutcome]) -> str:
    if any(o.status is OutcomeStatus.ERROR for o in outcomes):
        return "unavailable"
    if outcomes and all(o.status.withheld for o in outcomes):
        return "withheld"
    return "nilknown"


def _section(t: str, urns: list[str], outcomes: list[TypeOutcome], base: str) -> JsonObject:
    section: JsonObject = {"title": SECTION_TITLES.get(t, t), "code": {"text": t}}
    if outcomes:
        section["extension"] = [outcome_extension(o, base) for o in outcomes]
    if urns:
        section["entry"] = [{"reference": u} for u in urns]
    else:
        section["emptyReason"] = {"coding": [{"system": EMPTY_REASON, "code": _empty_reason(outcomes)}]}
    return section


def _documents_section(job: Job[A], idx: _Index, base: str) -> JsonObject:
    holders = sorted({r.referenced_by for r in job.documents.stored})
    urns = [idx.urns[h] for h in holders if h in idx.urns]
    section: JsonObject = {
        "title": "Document content",
        "code": {"text": DOCUMENTS_SECTION_CODE},
        "extension": [documents_extension(job.documents, base)],
    }
    if urns:
        section["entry"] = [{"reference": u} for u in urns]
    else:
        section["emptyReason"] = {"coding": [{"system": EMPTY_REASON, "code": "nilknown"}]}
    return section


def _absolute(url: str, iss: str) -> str:
    return url if "://" in url else f"{iss}/{url.lstrip('/')}"


def _inline_documents(
    resource: JsonObject, t: str, key: str, blobs: SpoolIndex, nonce: str, iss: str
) -> tuple[JsonObject, tuple[InlineBlob, ...]]:
    """Placeholders in place of `data` for every attachment the spool holds a blob for."""
    source = {s.resource_type: s for s in DocumentSource}.get(t)
    if source is None:
        return resource, ()
    field_name = "content" if source is DocumentSource.DOCUMENT_REFERENCE else "presentedForm"
    items = resource.get(field_name)
    if not isinstance(items, list):
        return resource, ()
    inline: list[InlineBlob] = []
    for i, item in enumerate(items):
        bid = blob_id(t, key, i)
        meta = blobs.blobs.get(bid)
        att = item.get("attachment") if source is DocumentSource.DOCUMENT_REFERENCE else item
        if meta is None or not isinstance(att, dict):
            continue
        placeholder = f"__patient_access_inline_{nonce}_{len(inline)}__"
        att["contentType"] = meta.content_type
        att["data"] = placeholder
        att["size"] = meta.size
        if isinstance(att.get("url"), str):
            att["url"] = _absolute(att["url"], iss)
        inline.append(InlineBlob(placeholder=placeholder, blob_id=bid))
    return resource, tuple(inline)


def document_bundle(
    job: Job[A], spool: SpoolHandle, settings: BundleSettings, now: datetime
) -> tuple[JsonObject, Iterator[Entry]]:
    """The Bundle's header (every field but `entry`) and its entries, Composition first."""
    base = settings.extension_base_url
    iss = job.source.iss.rstrip("/")
    blobs = spool.index()
    types = resource_types(job, blobs)
    idx = _collect(spool, types)
    nonce = uuid.uuid4().hex

    by_type: dict[str, list[TypeOutcome]] = {}
    for o in job.outcomes:
        by_type.setdefault(o.resource_type, []).append(o)

    device_id = str(uuid.uuid5(NS, f"author/{SOFTWARE}"))
    composition_id = str(uuid.uuid5(NS, f"composition/{job.id}"))
    patient_key = f"Patient/{job.source.ehr_patient_id}"
    composition: JsonObject = {
        "resourceType": "Composition",
        "id": composition_id,
        "extension": [pull_extension(job, base)],
        "status": "final",
        "type": {"coding": [LOINC_SUMMARY]},
        "subject": {"reference": idx.urns.get(patient_key, f"{iss}/{patient_key}")},
        "date": now.isoformat(),
        "author": [{"reference": f"urn:uuid:{device_id}"}],
        "title": f"Records shared from {job.source.brand_name}",
        "section": [
            *[_section(t, idx.sections.get(t, []), by_type.get(t, []), base) for t in types],
            _documents_section(job, idx, base),
        ],
    }
    source_uri = getattr(job.participant.attrs, "fhir_source_uri", None)
    if isinstance(source_uri, str) and source_uri:
        composition["meta"] = {"source": source_uri}
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
        yield Entry(full_url=f"urn:uuid:{composition_id}", resource=composition)
        yield Entry(full_url=f"urn:uuid:{device_id}", resource=device)
        for t in types:
            for position, raw in enumerate(spool.iter_type(t)):
                key = resource_key(raw, position)
                resource = rewrite_refs(raw, idx.urns, iss)
                resource, inline = _inline_documents(resource, t, key, blobs, nonce, iss)
                yield Entry(full_url=idx.urns[f"{t}/{key}"], resource=resource, inline=inline)

    return header, entries()
