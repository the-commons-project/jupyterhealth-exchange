"""The documents pass: every document's candidates through the budget and the downloader, one
record per attempt. A document's formats are tried best first until one is stored; a budget
refusal (the count or the total) stops the group, since no alternative could help, as does the
end of the EHR's authorization.

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
from .candidates import Candidate, candidate_groups
from .downloader import FetchResult, Skipped, Stored, fetch_one

SAVE_EVERY = 10
CAP_REASONS = frozenset({SkipReason.COUNT_CAP, SkipReason.SIZE_CAP_EACH, SkipReason.SIZE_CAP_TOTAL})
# After one of these, no other format of the same document could be stored either.
NO_ALTERNATIVE = frozenset({SkipReason.COUNT_CAP, SkipReason.SIZE_CAP_TOTAL, SkipReason.TOKEN_EXPIRED})


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
    stopped: Stop | None = None  # why the rest are skipped without a request

    attempts = 0
    for group in candidate_groups(spool, profile):
        if any(cand.blob_id in stored for cand in group):
            continue
        for cand in group:
            if (meta := spool.blob_meta(cand.blob_id)) is not None:
                # Committed by a run that died before saving its record, or earlier in this pass
                # by a candidate with the same blob (read live: a snapshot would not have it).
                result: FetchResult = Stored(meta=meta)
            elif stopped is not None:
                result = Skipped(reason=SkipReason.TOKEN_EXPIRED, detail=stopped.value)
            elif refusal := budget.refusal():
                result = Skipped(reason=refusal)
            else:
                attempts += 1
                try:
                    result = fetch_one(client, spool, cand, budget, caps)
                except TokenExpired:
                    stopped = Stop.EXPIRED
                    result = Skipped(reason=SkipReason.TOKEN_EXPIRED, detail=stopped.value)
                except Unauthorized:
                    stopped = Stop.UNAUTHORIZED
                    result = Skipped(reason=SkipReason.TOKEN_EXPIRED, detail=stopped.value)
                if attempts % SAVE_EVERY == 0:
                    save()
            out.record(record(cand, result))
            if isinstance(result, Stored):
                budget = budget.spend(result.meta.size)
                break
            if result.reason in NO_ALTERNATIVE:
                break

    if stopped is not None:
        out.status, out.error = OutcomeStatus.ERROR, "authorization ended during document download"
    else:
        out.status = OutcomeStatus.OK
    out.capped = any(reason in CAP_REASONS for reason in out.skipped)
    out.seconds = round(time.monotonic() - started, 2)
    log.info("documents: %d stored, %d skipped, %d bytes", len(out.stored), sum(out.skipped.values()), out.bytes)
    save()
    return stopped is None
