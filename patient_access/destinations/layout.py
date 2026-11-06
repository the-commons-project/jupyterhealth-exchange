"""TemplateLayout: where a pull lands in a bucket. The operator configures the prefix; the
module appends the pull's own part, so no layout can make one pull overwrite another.

    "{attrs.patient_id}/{attrs.fhir_source_id}"   (default)
    ->  40001/7/20261104T120000Z-3f9a1c2e/bundle.json

The appended part is the pull's start (UTC, to the second) and the first eight characters of the
job id, then `bundle.json`. Prefix fields: `participant.ref`, `participant.provider`,
`attrs.<name>` (the host's attributes, by field name) and `source.<name>` (`id`, a stable
path-safe health-system identifier; `vendor`, `brand_id`, `brand_name`, `facility_id`,
`customer_id`, `ehr_patient_id`). The prefix may be empty. Each `/` segment is rendered and
sanitised on its own, so no value can add or escape a segment.

The template is formatted against a flat table of plain strings, never against live objects:
Python's format strings follow any attribute chain, and from a job object that reaches the
process's environment (the encryption keys). Like Django's template lookup, a name with a leading
underscore, index access, conversions and format specs are refused outright.
"""

from __future__ import annotations

import re
import string
from collections.abc import Mapping, Sequence
from typing import Any

from patient_access.core.models import A, Job

SOURCE_FIELDS: frozenset[str] = frozenset(
    {"id", "vendor", "brand_id", "brand_name", "facility_id", "customer_id", "ehr_patient_id"}
)
PARTICIPANT_FIELDS: frozenset[str] = frozenset({"ref", "provider"})
TIME_FORMAT = "%Y%m%dT%H%M%SZ"
OBJECT_NAME = "bundle.json"
_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
_SAFE = re.compile(r"[^A-Za-z0-9._-]")


class LayoutError(ValueError):
    """The template is not usable, or this job lacks a field it names. Safe to show an operator."""


def safe(part: str) -> str:
    """One path segment: filename-safe characters only, never empty, never `.` or `..`."""
    out = _SAFE.sub("_", part)[:128]
    return out if out and out.strip(".") else "_"


def pull_part(job: Job[A]) -> str:
    """The segment the module appends for every pull: `<start>-<job id, first 8>`."""
    return f"{job.created_at.strftime(TIME_FORMAT)}-{safe(job.id[:8])}"


def _check_name(field: str) -> None:
    """A field is `root.name`: plain identifiers, no underscore at the start of any part, no
    index access, and a root the table knows."""
    if "[" in field or "]" in field:
        raise LayoutError(f"layout field {{{field}}} uses index access, which is not allowed")
    parts = field.split(".")
    if any(p.startswith("_") for p in parts):
        raise LayoutError(f"layout field {{{field}}} names something private (a leading underscore)")
    if not all(_NAME.fullmatch(p) for p in parts):
        raise LayoutError(f"layout field {{{field}}} is not a field name")
    root = parts[0]
    known = (
        (root == "participant" and len(parts) == 2 and parts[1] in PARTICIPANT_FIELDS)
        or (root == "source" and len(parts) == 2 and parts[1] in SOURCE_FIELDS)
        or (root == "attrs" and len(parts) == 2)
    )
    if not known:
        raise LayoutError(
            f"layout field {{{field}}} is unknown; the prefix may use participant.ref, participant.provider, "
            f"attrs.<name>, or source.<{'|'.join(sorted(SOURCE_FIELDS))}> (the pull's time and job id are "
            "appended by the module itself)"
        )


class _Fields(string.Formatter):
    """Looks a field up in a flat table by its whole name; never walks an attribute."""

    def __init__(self, values: Mapping[str, str]) -> None:
        self.values = values

    def get_field(self, field_name: str, args: Sequence[Any], kwargs: Mapping[str, Any]) -> tuple[Any, str]:
        if field_name not in self.values:
            raise LayoutError(f"layout field {{{field_name}}} is not available for this pull")
        return self.values[field_name], field_name

    def convert_field(self, value: Any, conversion: str | None) -> Any:
        if conversion:
            raise LayoutError("layout conversions (!r, !s, !a) are not allowed")
        return value


def _values(job: Job[A]) -> dict[str, str]:
    """Every value a prefix may name, as plain strings."""
    attrs = job.participant.attrs
    source = job.source
    out: dict[str, str] = {
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
        self.segments = self.template.split("/") if self.template else []
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
                if spec:
                    raise LayoutError(f"layout field {{{field}}} has a format, which is not allowed")

    def render(self, job: Job[A]) -> str:
        """The whole key: the rendered prefix, the pull's part, the object name."""
        formatter = _Fields(_values(job))
        out = []
        for seg in self.segments:
            try:
                out.append(safe(formatter.format(seg)))
            except LayoutError:
                raise
            except (KeyError, ValueError) as e:
                raise LayoutError(f"layout segment {seg!r} could not be rendered ({type(e).__name__})") from e
        return "/".join([*out, pull_part(job), OBJECT_NAME])
