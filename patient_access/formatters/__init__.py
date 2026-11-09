"""The delivered object: one FHIR document Bundle per pull, rendered as a byte stream."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime

from patient_access.core.interfaces import SpoolHandle
from patient_access.core.models import A, Job
from patient_access.core.settings import BundleSettings

from .document_bundle import document_bundle
from .stream import frame


def render_bundle(job: Job[A], spool: SpoolHandle, settings: BundleSettings, now: datetime) -> Iterator[bytes]:
    """The pipeline's renderer: the document Bundle's bytes, documents streamed in from the spool."""
    header, entries = document_bundle(job, spool, settings, now)
    return frame(header, entries, spool.iter_blob)
