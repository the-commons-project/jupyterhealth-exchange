"""The documents pass: every candidate through the budget and the downloader, one record each.

Returns whether it completed. When the EHR's authorization ends part-way (the token expires, or
the server answers 401), the remaining candidates are recorded as skipped without a request and
the pass returns False: the pipeline then fails the job rather than deliver part of the record.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from enum import StrEnum

from ..client import FhirClient, TokenExpired, Unauthorized
from ..interfaces import SpoolHandle
from ..log import JobLog
from ..models import A, DocumentRecord, DocumentResult, Job, OutcomeStatus, SkipReason
from ..settings import DocumentCaps
from ..source_settings import DocumentProfile
from .budget import Budget
from .candidates import Candidate, candidates
from .downloader import FetchResult, Skipped, Stored, fetch_one

SAVE_EVERY = 10
CAP_REASONS = frozenset({SkipReason.COUNT_CAP, SkipReason.SIZE_CAP_EACH, SkipReason.SIZE_CAP_TOTAL})


class Stop(StrEnum):
    """Why the pass stopped making requests."""

    EXPIRED = "expired"  # the token reached its expiry
    UNAUTHORIZED = "401"  # the server refused the token


def record(cand: Candidate, result: FetchResult) -> DocumentRecord:
    match result:
        case Stored(meta=meta):
            return DocumentRecord(
                blob_id=cand.blob_id,
                source=cand.source,
                referenced_by=cand.referenced_by,
                attachment_index=cand.attachment_index,
                content_type=meta.content_type,
                result=DocumentResult.STORED,
                size=meta.size,
                sha256=meta.sha256,
            )
        case Skipped(reason=reason, detail=detail):
            return DocumentRecord(
                blob_id=cand.blob_id,
                source=cand.source,
                referenced_by=cand.referenced_by,
                attachment_index=cand.attachment_index,
                content_type=cand.content_type,
                result=DocumentResult.SKIPPED,
                reason=reason,
                detail=detail,
            )


def fetch_documents(
    job: Job[A],
    spool: SpoolHandle,
    profile: DocumentProfile,
    caps: DocumentCaps,
    client: FhirClient,
    save: Callable[[], None],
    log: JobLog,
) -> bool:
    started = time.monotonic()
    out = job.documents
    out.status, out.error = OutcomeStatus.PENDING, None
    # Resume: what earlier runs stored is already spent.
    stored = {r.blob_id: r for r in out.stored if spool.has_blob(r.blob_id)}
    budget = Budget(caps=caps, count=len(stored), bytes=sum(r.size or 0 for r in stored.values()))
    index = spool.index()
    stopped: Stop | None = None  # why the rest are skipped without a request

    for n, cand in enumerate(candidates(spool, profile), start=1):
        if cand.blob_id in stored:
            continue
        if spool.has_blob(cand.blob_id):
            # Committed by a run that died before saving its record.
            meta = index.blobs[cand.blob_id]
            out.record(record(cand, Stored(meta=meta)))
            budget = budget.spend(meta.size)
            continue
        if stopped is not None:
            out.record(record(cand, Skipped(reason=SkipReason.TOKEN_EXPIRED, detail=stopped.value)))
            continue
        if refusal := budget.refusal():
            out.record(record(cand, Skipped(reason=refusal)))
            continue
        try:
            result = fetch_one(client, spool, cand, budget, caps.max_binary_json_bytes)
        except TokenExpired:
            stopped = Stop.EXPIRED
            result = Skipped(reason=SkipReason.TOKEN_EXPIRED, detail=stopped.value)
        except Unauthorized:
            stopped = Stop.UNAUTHORIZED
            result = Skipped(reason=SkipReason.TOKEN_EXPIRED, detail=stopped.value)
        out.record(record(cand, result))
        if isinstance(result, Stored):
            budget = budget.spend(result.meta.size)
        if n % SAVE_EVERY == 0:
            save()

    if stopped is not None:
        out.status, out.error = OutcomeStatus.ERROR, "authorization ended during document download"
    else:
        out.status = OutcomeStatus.OK
    out.capped = any(reason in CAP_REASONS for reason in out.skipped)
    out.seconds = round(time.monotonic() - started, 2)
    log.info("documents: %d stored, %d skipped, %d bytes", len(out.stored), sum(out.skipped.values()), out.bytes)
    save()
    return stopped is None
