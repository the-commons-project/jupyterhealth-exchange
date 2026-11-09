#!/usr/bin/env python3
"""Check a USCDI Patient Access bundle against its specification (patient_access/docs/patient-access-bundle.md).

    python patient_access/scripts/validate_patient_access_bundle.py FILE [FILE ...]

Standard library only, so it runs anywhere Python 3.11 does. Prints a report per file and exits
1 if any check failed. The checks are independent of the code that writes the bundles: they read
the file as a consumer would, by the rules the specification states.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import sys
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PULL_EXTENSION = "/patient-access-pull"
SECTION_OUTCOME_EXTENSION = "/patient-access-section-outcome"
DOCUMENTS_OUTCOME_EXTENSION = "/patient-access-documents-outcome"
DOCUMENTS_SECTION_CODE = "documents"
LOINC_SUMMARY = "60591-5"
EMPTY_REASON_SYSTEM = "http://terminology.hl7.org/CodeSystem/list-empty-reason"
GENERATED_MARKER = "it is not a record from the healthcare organization"

OUTCOME_STATUSES = {"ok", "error", "skipped", "not_requested", "not_granted"}
EMPTY_REASONS = {"nilknown", "withheld", "unavailable"}
DOCUMENT_RESULTS = {"stored", "skipped"}
SKIP_REASONS = {
    "count_cap",
    "size_cap_each",
    "size_cap_total",
    "binary_json_cap",
    "no_content",
    "bad_inline_data",
    "url_refused",
    "http_error",
    "network",
    "unreadable_binary",
    "token_expired",
    "too_slow",
    "spool_corrupt",
}
DOCUMENT_SOURCES = {"DocumentReference.content": "content", "DiagnosticReport.presentedForm": "presentedForm"}
URN = re.compile(r"^urn:uuid:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
TYPE_ORDER = [
    "Patient",
    "Condition",
    "MedicationRequest",
    "MedicationDispense",
    "AllergyIntolerance",
    "Immunization",
    "Procedure",
    "Observation",
    "DiagnosticReport",
    "DocumentReference",
    "Encounter",
    "CarePlan",
    "CareTeam",
    "Goal",
    "ServiceRequest",
    "Device",
    "QuestionnaireResponse",
]

Json = dict[str, Any]


@dataclass
class Report:
    path: Path
    checks: list[tuple[bool, str, str]] = field(default_factory=list)
    sections: list[tuple[str, int, str]] = field(default_factory=list)

    def check(self, ok: bool, name: str, detail: str = "") -> bool:
        self.checks.append((ok, name, detail))
        return ok

    @property
    def failed(self) -> list[tuple[bool, str, str]]:
        return [c for c in self.checks if not c[0]]


# --- small readers ---------------------------------------------------------------------------


def _value(e: Json) -> Any:
    for k in ("valueString", "valueInteger", "valueBoolean", "valueDecimal", "valueDateTime"):
        if k in e:
            return e[k]
    return None


def _children(e: Json) -> list[Json]:
    kids = e.get("extension")
    return [c for c in kids if isinstance(c, dict)] if isinstance(kids, list) else []


def _child(children: list[Json], url: str) -> Json:
    return next((c for c in children if c.get("url") == url), {})


def _values(children: list[Json], url: str) -> list[Any]:
    return [_value(c) for c in children if c.get("url") == url]


def _first(children: list[Json], url: str) -> Any:
    found = _values(children, url)
    return found[0] if found else None


def _ending(extensions: Any, suffix: str) -> Iterator[Json]:
    for e in extensions if isinstance(extensions, list) else []:
        if isinstance(e, dict) and str(e.get("url", "")).endswith(suffix):
            yield e


def _references(node: Any) -> Iterator[str]:
    if isinstance(node, dict):
        ref = node.get("reference")
        if isinstance(ref, str):
            yield ref
        for v in node.values():
            yield from _references(v)
    elif isinstance(node, list):
        for v in node:
            yield from _references(v)


def _attachments(resource: Json) -> list[Json]:
    """The attachment objects of a document-bearing resource, by index."""
    rtype = resource.get("resourceType")
    if rtype == "DocumentReference":
        items = resource.get("content")
        return (
            [i.get("attachment", {}) if isinstance(i, dict) else {} for i in items] if isinstance(items, list) else []
        )
    if rtype == "DiagnosticReport":
        items = resource.get("presentedForm")
        return [i if isinstance(i, dict) else {} for i in items] if isinstance(items, list) else []
    return []


# --- the checks ------------------------------------------------------------------------------


def _bad_constant(name: str) -> None:
    raise ValueError(f"non-finite number {name}")


def validate(path: Path) -> Report:
    r = Report(path)
    raw = path.read_bytes()
    r.check(raw.isascii(), "file is ASCII-only UTF-8")
    try:
        bundle = json.loads(raw, parse_constant=_bad_constant)
    except ValueError as e:
        r.check(False, "file is strict JSON", str(e))
        return r
    r.check(True, "file is strict JSON (no NaN or Infinity)")
    if not isinstance(bundle, dict):
        r.check(False, "top level is an object")
        return r

    # 2. the Bundle
    r.check(bundle.get("resourceType") == "Bundle" and bundle.get("type") == "document", "Bundle of type document")
    r.check(
        bool(bundle.get("id")) and bool((bundle.get("identifier") or {}).get("value")), "Bundle has id and identifier"
    )
    r.check(isinstance(bundle.get("timestamp"), str), "Bundle has a timestamp")
    entries = bundle.get("entry")
    if not (isinstance(entries, list) and entries):
        r.check(False, "Bundle has entries")
        return r
    entries = [e for e in entries if isinstance(e, dict)]
    full_urls = [e.get("fullUrl") for e in entries]
    r.check(all(isinstance(u, str) and URN.match(u) for u in full_urls), "every entry has a urn:uuid fullUrl")
    dupes = [u for u, n in Counter(full_urls).items() if n > 1]
    r.check(not dupes, "fullUrls are unique", f"{len(dupes)} repeated" if dupes else "")
    resources = [e.get("resource") for e in entries]
    r.check(
        all(isinstance(x, dict) and isinstance(x.get("resourceType"), str) for x in resources),
        "every entry has a resource with a resourceType",
    )
    by_url: dict[str, Json] = {u: x for u, x in zip(full_urls, resources) if isinstance(u, str) and isinstance(x, dict)}
    typed = [(x.get("resourceType"), x.get("id")) for x in resources if isinstance(x, dict)]
    with_ids = [t for t in typed if t[1] is not None]
    dup_ids = [t for t, n in Counter(with_ids).items() if n > 1]
    r.check(not dup_ids, "no resource type/id appears twice", ", ".join(f"{a}/{b}" for a, b in dup_ids[:3]))
    without_id = sum(1 for t in typed if t[1] is None and t[0] not in ("Composition", "Device", "Binary"))
    r.check(True, "resources without an id", f"{without_id}" if without_id else "none")

    composition = resources[0] if resources and isinstance(resources[0], dict) else {}
    r.check(composition.get("resourceType") == "Composition", "entry[0] is the Composition")
    device = resources[1] if len(resources) > 1 and isinstance(resources[1], dict) else {}
    r.check(device.get("resourceType") == "Device", "entry[1] is the Device (the software)")
    types_in_order = [x.get("resourceType") for x in resources if isinstance(x, dict)]
    first_binary = next((i for i, t in enumerate(types_in_order) if t == "Binary"), len(types_in_order))
    r.check(all(t == "Binary" for t in types_in_order[first_binary:]), "Binary entries come last")

    # 3. the Composition
    r.check(composition.get("status") == "final", "Composition.status is final")
    codings = (composition.get("type") or {}).get("coding") or []
    r.check(
        any(c.get("code") == LOINC_SUMMARY for c in codings if isinstance(c, dict)), "Composition.type is LOINC 60591-5"
    )
    subject = (composition.get("subject") or {}).get("reference")
    r.check(
        isinstance(subject, str)
        and (subject in by_url or subject.startswith("https://") or subject.startswith("http://")),
        "Composition.subject resolves (a Patient entry, or the organization's absolute URL)",
        str(subject),
    )
    author = ((composition.get("author") or [{}])[0] or {}).get("reference")
    r.check(author == full_urls[1] if len(full_urls) > 1 else False, "Composition.author is the Device entry")
    r.check(composition.get("date") == bundle.get("timestamp"), "Composition.date equals Bundle.timestamp")

    pull = next(_ending(composition.get("extension"), PULL_EXTENSION), None)
    if not r.check(pull is not None, "Composition carries the patient-access-pull extension"):
        return r
    parts = _children(pull or {})
    job = _children(_child(parts, "job"))
    participant = _children(_child(parts, "participant"))
    source = _children(_child(parts, "source"))
    scopes = _children(_child(parts, "scopes"))
    software = _children(_child(parts, "software"))
    r.check(
        bool(_first(job, "id")) and bool(_first(job, "created")) and bool(_first(job, "finished")),
        "job: id, created, finished",
    )
    r.check(_first(job, "finished") == bundle.get("timestamp"), "job.finished equals Bundle.timestamp")
    r.check(bool(_first(participant, "ref")) and bool(_first(participant, "provider")), "participant: ref, provider")
    r.check(
        bool(_first(source, "brand")) and bool(_first(source, "iss")) and bool(_first(source, "ehrPatientId")),
        "source: brand, iss, ehrPatientId",
    )
    r.check(
        _first(source, "typeChoice") in ("host", "ehr"),
        "source.typeChoice is host or ehr",
        str(_first(source, "typeChoice")),
    )
    r.check(
        bool(_values(scopes, "requested")) and bool(_values(scopes, "granted")), "scopes: requested and granted present"
    )
    r.check(bool(_first(software, "name")) and bool(_first(software, "version")), "software: name, version")

    # 3.2 / 3.3 sections
    sections = [s for s in (composition.get("section") or []) if isinstance(s, dict)]
    r.check(bool(sections), "Composition has sections")
    codes = [str((s.get("code") or {}).get("text")) for s in sections]
    r.check(codes[-1:] == [DOCUMENTS_SECTION_CODE], "the documents section is last")
    planned = [c for c in codes if c in TYPE_ORDER]
    r.check(planned == sorted(planned, key=TYPE_ORDER.index), "record sections are in the specified order")
    referenced_by_section: Counter[str] = Counter()
    section_refs_ok = True
    for s, code in zip(sections, codes):
        refs = [e.get("reference") for e in (s.get("entry") or []) if isinstance(e, dict)]
        has_text, has_entries = "text" in s, bool(refs)
        if has_text == has_entries:
            r.check(False, f"section {code}: text xor entries (R4 cmp-1)", f"text={has_text} entries={has_entries}")
        if not has_entries:
            reason = ((s.get("emptyReason") or {}).get("coding") or [{}])[0]
            r.check(
                reason.get("system") == EMPTY_REASON_SYSTEM and reason.get("code") in EMPTY_REASONS,
                f"section {code}: emptyReason from the vocabulary",
                str(reason.get("code")),
            )
            text = s.get("text") or {}
            r.check(
                text.get("status") == "generated" and GENERATED_MARKER in str(text.get("div")),
                f"section {code}: generated narrative marked as the software's",
            )
        elif "emptyReason" in s:
            r.check(False, f"section {code}: emptyReason on a section with entries")
        dup_refs = [u for u, n in Counter(refs).items() if n > 1]
        if dup_refs:
            r.check(False, f"section {code}: no duplicate references", f"{len(dup_refs)}")
        expected_type = "Binary" if code == DOCUMENTS_SECTION_CODE else code
        wrong = [
            u
            for u in refs
            if not (isinstance(u, str) and u in by_url and by_url[u].get("resourceType") == expected_type)
        ]
        if wrong:
            section_refs_ok = False
            r.check(False, f"section {code}: every reference resolves to a {expected_type}", f"{len(wrong)} do not")
        for u in refs:
            if isinstance(u, str):
                referenced_by_section[u] += 1
        outcomes = list(_ending(s.get("extension"), SECTION_OUTCOME_EXTENSION))
        if code != DOCUMENTS_SECTION_CODE:
            statuses, counts = [], 0
            for o in outcomes:
                oc = _children(o)
                status, count = _first(oc, "status"), _first(oc, "count")
                statuses.append(str(status))
                if status not in OUTCOME_STATUSES:
                    r.check(False, f"section {code}: outcome status from the vocabulary", str(status))
                if not (isinstance(count, int) and count >= 0):
                    r.check(False, f"section {code}: outcome count is a non-negative integer", str(count))
                elif status == "ok":
                    counts += count
            if outcomes:
                r.check(
                    counts == len(refs),
                    f"section {code}: entries equal the sum of its ok counts",
                    f"{len(refs)} entries, counts {counts}",
                )
            r.sections.append((code, len(refs), ",".join(statuses) or "(no outcome)"))
        else:
            r.sections.append((code, len(refs), "documents"))
    r.check(section_refs_ok, "section references all resolve to entries of the section's type")

    # every resource (not the Composition, the Device, or a Binary) is in exactly one section
    unreferenced, multiply = [], []
    for u, x in by_url.items():
        if u == full_urls[0] or u == (full_urls[1] if len(full_urls) > 1 else None):
            continue
        n = referenced_by_section.get(u, 0)
        if n == 0:
            unreferenced.append(f"{x.get('resourceType')}/{x.get('id')}")
        elif n > 1:
            multiply.append(f"{x.get('resourceType')}/{x.get('id')}")
    r.check(not unreferenced, "every resource is listed in a section", ", ".join(unreferenced[:3]))
    r.check(not multiply, "no resource is listed in two sections", ", ".join(multiply[:3]))
    for t, n in Counter(x.get("resourceType") for x in by_url.values()).items():
        if t in ("Composition", "Device") and n == 1:
            continue
        listed = sum(1 for u, x in by_url.items() if x.get("resourceType") == t and referenced_by_section.get(u))
        if t == "Device":
            listed = sum(1 for u, x in by_url.items() if x.get("resourceType") == t and referenced_by_section.get(u))
            n -= 1  # the software's Device entry is not a record
        if listed != n:
            r.check(False, f"{t}: entries in the bundle equal entries listed in its section", f"{n} vs {listed}")

    # 4. references and attachments
    dangling = [
        ref for x in by_url.values() for ref in _references(x) if ref.startswith("urn:uuid:") and ref not in by_url
    ]
    r.check(not dangling, "every urn:uuid reference resolves", f"{len(dangling)} dangling")
    relative = [
        ref
        for x in by_url.values()
        for ref in _references(x)
        if not ref.startswith(("urn:", "#", "http://", "https://"))
    ]
    r.check(not relative, "no reference is relative", f"{len(relative)}: {relative[:2]}")

    # 4.2 / 5 documents
    docs_section = sections[-1] if codes[-1:] == [DOCUMENTS_SECTION_CODE] else {}
    docs_ext = next(_ending(docs_section.get("extension"), DOCUMENTS_OUTCOME_EXTENSION), None)
    binaries = {u: x for u, x in by_url.items() if x.get("resourceType") == "Binary"}
    if r.check(docs_ext is not None, "documents section carries the documents outcome"):
        dc = _children(docs_ext or {})
        records = [_children(d) for d in dc if d.get("url") == "document"]
        stored = [rc for rc in records if _first(rc, "result") == "stored"]
        r.check(
            _first(dc, "stored") == len(binaries) == len(stored),
            "stored equals the Binary entries equals the stored records",
            f"{_first(dc, 'stored')} / {len(binaries)} / {len(stored)}",
        )
        r.check(all(_first(rc, "result") in DOCUMENT_RESULTS for rc in records), "document results from the vocabulary")
        bad_reasons = [
            str(_first(rc, "reason"))
            for rc in records
            if _first(rc, "result") == "skipped" and _first(rc, "reason") not in SKIP_REASONS
        ]
        r.check(not bad_reasons, "skip reasons from the vocabulary", ", ".join(bad_reasons[:3]))
        skipped_counts = sum(int(_first(_children(d), "count") or 0) for d in dc if d.get("url") == "skipped")
        r.check(
            skipped_counts == len(records) - len(stored),
            "skipped counts equal the skipped records",
            f"{skipped_counts} vs {len(records) - len(stored)}",
        )
        total_bytes = 0.0
        problems: list[str] = []
        pointed: set[str] = set()
        for rc in records:
            referenced_by = str(_first(rc, "referencedBy"))
            rtype, _, rid = referenced_by.partition("/")
            target = next((x for x in by_url.values() if x.get("resourceType") == rtype and x.get("id") == rid), None)
            index = _first(rc, "attachment")
            att = (
                _attachments(target)[index]
                if target is not None and isinstance(index, int) and index < len(_attachments(target))
                else None
            )
            if att is None:
                problems.append(f"{referenced_by}[{index}] not in the bundle")
                continue
            if _first(rc, "result") == "stored":
                binary_url, size, sha = _first(rc, "binary"), _first(rc, "size"), _first(rc, "sha256")
                binary = binaries.get(str(binary_url))
                if binary is None:
                    problems.append(f"{referenced_by}[{index}]: no Binary entry at {binary_url}")
                    continue
                try:
                    data = base64.b64decode(binary.get("data", ""), validate=True)
                except ValueError:
                    problems.append(f"{referenced_by}[{index}]: Binary data is not base64")
                    continue
                if len(data) != size or hashlib.sha256(data).hexdigest() != sha:
                    problems.append(f"{referenced_by}[{index}]: bytes do not match size/sha256")
                if binary.get("contentType") != _first(rc, "contentType"):
                    problems.append(f"{referenced_by}[{index}]: contentType differs from the record")
                if att.get("url") != binary_url or "data" in att:
                    problems.append(
                        f"{referenced_by}[{index}]: attachment does not point at its Binary (or still carries data)"
                    )
                pointed.add(str(binary_url))
                total_bytes += len(data)
            elif "data" in att:
                problems.append(f"{referenced_by}[{index}]: a skipped attachment still carries inline data")
        r.check(
            not problems, "every document record is consistent with its attachment and Binary", "; ".join(problems[:3])
        )
        r.check(
            set(binaries) == pointed,
            "every Binary entry is pointed at by exactly its attachment",
            f"{len(binaries)} vs {len(pointed)}",
        )
        declared = _first(dc, "bytes")
        r.check(
            declared is None or float(declared) == total_bytes,
            "documents bytes equals the decoded total",
            f"{declared} vs {total_bytes}",
        )
    attachments_with_urn = [
        a
        for x in by_url.values()
        for a in _attachments(x)
        if str(a.get("url", "")).startswith("urn:uuid:") and a.get("url") not in binaries
    ]
    r.check(not attachments_with_urn, "every attachment urn points at a Binary entry", f"{len(attachments_with_urn)}")
    return r


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    exit_code = 0
    for arg in argv:
        report = validate(Path(arg))
        print(f"== {report.path}")
        for ok, name, detail in report.checks:
            print(f"  {'PASS' if ok else 'FAIL'}  {name}{'  (' + detail + ')' if detail else ''}")
        if report.sections:
            print("  sections:")
            for code, n, statuses in report.sections:
                print(f"    {code:<24} {n:>6}  {statuses}")
        failed = report.failed
        print(f"  {len(report.checks) - len(failed)}/{len(report.checks)} checks passed")
        if failed:
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
