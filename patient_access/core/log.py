"""The logging rule: a job's log lines carry ids, phase, status, counts and timings, never PHI.

Never logged: a request URL with its query string, a response body, a token, or EHR error text
verbatim (an OperationOutcome can carry patient details). EHR error text is kept on the outcome
record and in the bundle's provenance; the log gets its HTTP status and `sanitised` prefix.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, MutableMapping
from typing import Any

from .models import A, Job

LOGGER_NAME = "patient_access"
log = logging.getLogger(LOGGER_NAME)

_KEEP = re.compile(r"[^A-Za-z .,:;()_-]+")
_SPACES = re.compile(r"\s+")
SANITISED_LENGTH = 60


def sanitised(text: str | None, limit: int = SANITISED_LENGTH) -> str:
    """Letters and punctuation only (digits go: they are what identifies people and records),
    whitespace collapsed, at most `limit` characters."""
    if not text:
        return ""
    return _SPACES.sub(" ", _KEEP.sub(" ", text)).strip()[:limit]


class JobLog(logging.LoggerAdapter[logging.Logger]):
    """Prefixes every message with the job id and the participant ref, and puts them, plus
    phase and resource type when given, in the record's `extra` for structured handlers."""

    def process(self, msg: str, kwargs: MutableMapping[str, Any]) -> tuple[str, MutableMapping[str, Any]]:
        extra: Mapping[str, object] = self.extra or {}
        kwargs["extra"] = {**extra, **kwargs.get("extra", {})}
        return f"job {extra.get('job_id')} [{extra.get('phase', '-')}] {msg}", kwargs


def job_log(job: Job[A], phase: str) -> JobLog:
    return JobLog(log, {"job_id": job.id, "participant_ref": job.participant.ref, "phase": phase})
