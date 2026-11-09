"""The logging rule: a job's log lines carry ids, phase, status, counts and timings, never PHI.

Never logged: a request URL with its query string, a response body, a token, or EHR error text
verbatim (an OperationOutcome can carry patient details). EHR error text is kept on the outcome
record and in the bundle's provenance; the log gets its HTTP status and `sanitised` prefix.

Unexpected exceptions go through `log_failure`, never `log.exception`: an exception's arguments
can carry data (a validation error quotes the input it rejected, a KeyError names the key), so
the log gets the type, a sanitised message and the traceback's frames, which say where it failed
without what the data was.

The HTTP library's own request lines (urllib3, at DEBUG) carry query strings with the EHR
patient id; the Django app pins those loggers to WARNING unless `HTTP_DEBUG_LOGGING` is on.
"""

from __future__ import annotations

import logging
import re
import traceback
from collections.abc import Mapping, MutableMapping
from enum import StrEnum
from typing import Any

from pydantic import ValidationError

from .models import A, Job, JobId

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


class Phase(StrEnum):
    RUN = "run"
    FETCH = "fetch"
    DELIVER = "deliver"
    END = "end"
    LEASE = "lease"


def _detail(e: BaseException) -> str:
    """What may be said about an exception. A lookup error's message is the key it missed (a
    resource id); a validation error quotes the input it rejected. Those say only where."""
    if isinstance(e, ValidationError):
        return "at " + ", ".join(".".join(str(p) for p in err["loc"]) for err in e.errors()[:5])
    if isinstance(e, LookupError):
        return ""
    return sanitised(str(e))


def log_failure(logger: logging.Logger | logging.LoggerAdapter[logging.Logger], message: str, e: BaseException) -> None:
    """An unexpected exception without its arguments: type, a safe detail, traceback frames."""
    frames = "".join(traceback.format_tb(e.__traceback__)).rstrip()
    logger.error("%s: %s %s\n%s", message, type(e).__name__, _detail(e), frames)


class JobLog(logging.LoggerAdapter[logging.Logger]):
    """Prefixes every message with the job id and the participant ref, and puts them, plus
    phase and resource type when given, in the record's `extra` for structured handlers."""

    def process(self, msg: str, kwargs: MutableMapping[str, Any]) -> tuple[str, MutableMapping[str, Any]]:
        extra: Mapping[str, object] = self.extra or {}
        kwargs["extra"] = {**extra, **kwargs.get("extra", {})}
        return f"job {extra.get('job_id')} [{extra.get('phase', '-')}] {msg}", kwargs


def job_log(job: Job[A], phase: Phase) -> JobLog:
    return JobLog(log, {"job_id": job.id, "participant_ref": job.participant.ref, "phase": phase.value})


def lease_log(job_id: JobId) -> JobLog:
    """For the runner, which holds a job id and a lease but not the job."""
    return JobLog(log, {"job_id": job_id, "phase": Phase.LEASE.value})
