"""TemplateLayout: where a pull lands in a bucket, as a template the operator configures.

    "{attrs.patient_id}/{attrs.fhir_source_id}/{created_at:%Y%m%dT%H%M%SZ}/bundle.json"   (default)

Roots: `participant` (ref, provider), `attrs` (the host's attributes, by field name), `job` (id),
`source` (id: a stable, path-safe health-system identifier; also vendor, brand_name, facility_id,
customer_id, ehr_patient_id), `created_at` (the pull's start, a UTC datetime). The template
renders the whole key, file name included. Each `/` segment is rendered and sanitised on its own,
so no value can add or escape a prefix. A template must contain a per-pull part (`{job.id}` or a
`{created_at...}` field), or a second pull would overwrite the first.
"""

from __future__ import annotations

import re
import string
from typing import Any

from patient_access.core.models import A, Job

ROOTS: frozenset[str] = frozenset({"participant", "attrs", "job", "source", "created_at"})
_SAFE = re.compile(r"[^A-Za-z0-9._-]")


class LayoutError(ValueError):
    """The template is not usable, or this job lacks a field it names. Safe to show an operator."""


def safe(part: str) -> str:
    """One path segment: filename-safe characters only, never empty, never `.` or `..`."""
    out = _SAFE.sub("_", part)[:128]
    return out if out and out.strip(".") else "_"


def _fields(segment: str) -> list[str]:
    return [f for _, f, _, _ in string.Formatter().parse(segment) if f is not None]


class TemplateLayout:
    def __init__(self, template: str) -> None:
        self.template = template.strip().strip("/")
        if not self.template:
            raise LayoutError("the layout is empty")
        self.segments = self.template.split("/")
        fields: list[str] = []
        for seg in self.segments:
            if not seg:
                raise LayoutError(f"layout {template!r} has an empty segment")
            try:
                fields += _fields(seg)
            except ValueError as e:
                raise LayoutError(f"layout segment {seg!r} is not a valid template: {e}") from None
        for field in fields:
            root = re.split(r"[.\[:!]", field, maxsplit=1)[0]
            if root not in ROOTS:
                raise LayoutError(f"layout field {{{field}}} is unknown; roots are {', '.join(sorted(ROOTS))}")
        if not any(f == "job.id" or f.startswith("created_at") for f in fields):
            raise LayoutError(
                "the layout needs a per-pull part ({job.id} or {created_at:...}), or pulls overwrite each other"
            )

    def render(self, job: Job[A]) -> str:
        ctx: dict[str, Any] = {
            "participant": job.participant,
            "attrs": job.participant.attrs,
            "job": job,
            "source": job.source,
            "created_at": job.created_at,
        }
        out = []
        for seg in self.segments:
            try:
                out.append(safe(seg.format_map(ctx)))
            except (KeyError, AttributeError, IndexError, ValueError) as e:
                raise LayoutError(f"layout segment {seg!r} needs {e}, which this pull does not have") from e
        return "/".join(out)
