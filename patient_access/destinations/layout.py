"""TemplateLayout: where a pull lands in a bucket, as a template the operator configures.

    "{attrs.patient_id}/{attrs.fhir_source_id}/{created_at:%Y%m%dT%H%M%SZ}/bundle.json"   (default)

Fields: `participant.ref`, `participant.provider`, `attrs.<name>` (the host's attributes, by field
name), `job.id`, `source.<name>` (`id`, a stable path-safe health-system identifier; `vendor`,
`brand_id`, `brand_name`, `facility_id`, `customer_id`, `ehr_patient_id`), and `created_at` (the
pull's start, a UTC datetime, the only field that takes a format spec). The template renders the
whole key, file name included. Each `/` segment is rendered and sanitised on its own, so no value
can add or escape a prefix.

The template is formatted against a flat table of plain values, never against live objects:
Python's format strings follow any attribute chain, and from a job object that reaches the
process's environment (the encryption keys). Like Django's template lookup, a name with a leading
underscore, index access and conversions are refused outright.

A template must contain a per-pull part, `{job.id}` or a `{created_at...}` precise to the second,
or a later pull would overwrite an earlier one (a layout with only the year would keep one pull a
year).
"""

from __future__ import annotations

import re
import string
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from patient_access.core.models import A, Job

SOURCE_FIELDS: frozenset[str] = frozenset(
    {"id", "vendor", "brand_id", "brand_name", "facility_id", "customer_id", "ehr_patient_id"}
)
PARTICIPANT_FIELDS: frozenset[str] = frozenset({"ref", "provider"})
TIME_FIELD = "created_at"
SECONDS_DIRECTIVES = ("%S", "%T", "%s")  # a created_at format at least this precise is per pull
_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
_SAFE = re.compile(r"[^A-Za-z0-9._-]")

Rendered = str | datetime


class LayoutError(ValueError):
    """The template is not usable, or this job lacks a field it names. Safe to show an operator."""


def safe(part: str) -> str:
    """One path segment: filename-safe characters only, never empty, never `.` or `..`."""
    out = _SAFE.sub("_", part)[:128]
    return out if out and out.strip(".") else "_"


def _check_name(field: str) -> None:
    """A field is `root.name` (or `created_at`): plain identifiers, no underscore at the start of
    any part, no index access."""
    if "[" in field or "]" in field:
        raise LayoutError(f"layout field {{{field}}} uses index access, which is not allowed")
    parts = field.split(".")
    if any(p.startswith("_") for p in parts):
        raise LayoutError(f"layout field {{{field}}} names something private (a leading underscore)")
    if not all(_NAME.fullmatch(p) for p in parts):
        raise LayoutError(f"layout field {{{field}}} is not a field name")
    root = parts[0]
    known = (
        (root == TIME_FIELD and len(parts) == 1)
        or (root == "job" and parts[1:] == ["id"])
        or (root == "participant" and len(parts) == 2 and parts[1] in PARTICIPANT_FIELDS)
        or (root == "source" and len(parts) == 2 and parts[1] in SOURCE_FIELDS)
        or (root == "attrs" and len(parts) == 2)
    )
    if not known:
        raise LayoutError(
            f"layout field {{{field}}} is unknown; use created_at, job.id, participant.ref, "
            f"participant.provider, attrs.<name>, or source.<{'|'.join(sorted(SOURCE_FIELDS))}>"
        )


class _Fields(string.Formatter):
    """Looks a field up in a flat table by its whole name; never walks an attribute."""

    def __init__(self, values: Mapping[str, Rendered]) -> None:
        self.values = values

    def get_field(self, field_name: str, args: Sequence[Any], kwargs: Mapping[str, Any]) -> tuple[Any, str]:
        if field_name not in self.values:
            raise LayoutError(f"layout field {{{field_name}}} is not available for this pull")
        return self.values[field_name], field_name

    def convert_field(self, value: Any, conversion: str | None) -> Any:
        if conversion:
            raise LayoutError("layout conversions (!r, !s, !a) are not allowed")
        return value


def _values(job: Job[A]) -> dict[str, Rendered]:
    """Every value a template may name, as plain strings (the time stays a datetime, to format)."""
    attrs = job.participant.attrs
    source = job.source
    out: dict[str, Rendered] = {
        TIME_FIELD: job.created_at,
        "job.id": job.id,
        "participant.ref": job.participant.ref,
        "participant.provider": job.participant.provider,
        "source.id": source.id,
    }
    for name in sorted(SOURCE_FIELDS - {"id"}):
        value = getattr(source, name)
        if value is not None:
            out[f"source.{name}"] = str(value)
    for name in type(attrs).model_fields:
        value = getattr(attrs, name)
        if value is not None and not isinstance(value, list | tuple | dict | set):
            out[f"attrs.{name}"] = str(value)
    return out


class TemplateLayout:
    def __init__(self, template: str) -> None:
        self.template = template.strip().strip("/")
        if not self.template:
            raise LayoutError("the layout is empty")
        self.segments = self.template.split("/")
        per_pull = False
        for seg in self.segments:
            if not seg:
                raise LayoutError(f"layout {template!r} has an empty segment")
            try:
                parsed = list(string.Formatter().parse(seg))
            except ValueError as e:
                raise LayoutError(f"layout segment {seg!r} is not a valid template: {e}") from None
            for _, field, spec, conversion in parsed:
                if field is None:
                    continue
                _check_name(field)
                if conversion:
                    raise LayoutError(f"layout field {{{field}}} uses a conversion, which is not allowed")
                if spec and field != TIME_FIELD:
                    raise LayoutError(f"layout field {{{field}}} has a format; only {TIME_FIELD} takes one")
                if spec and "{" in spec:
                    raise LayoutError(f"layout field {{{field}}} nests a field in its format")
                if field == "job.id" or (
                    field == TIME_FIELD and (not spec or any(d in spec for d in SECONDS_DIRECTIVES))
                ):
                    per_pull = True
        if not per_pull:
            raise LayoutError(
                "the layout needs a per-pull part: {job.id}, or {created_at} with seconds (e.g. %Y%m%dT%H%M%SZ); "
                "otherwise a later pull overwrites an earlier one"
            )

    def render(self, job: Job[A]) -> str:
        formatter = _Fields(_values(job))
        out = []
        for seg in self.segments:
            try:
                out.append(safe(formatter.format(seg)))
            except LayoutError:
                raise
            except (KeyError, ValueError) as e:
                raise LayoutError(f"layout segment {seg!r} could not be rendered ({type(e).__name__})") from e
        return "/".join(out)
