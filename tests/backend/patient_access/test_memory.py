"""Peak memory of the whole document path at multi-gigabyte size: documents downloaded from a
streaming server (raw bytes, and one wrapped as a FHIR Binary at that path's cap) into the
encrypted spool, then verified, rendered as a bundle with Binary entries, digested and uploaded in
parts through the S3 sink.

Opt-in (it writes and reads gigabytes): PATIENT_ACCESS_SLOW=1, size in PATIENT_ACCESS_SLOW_GB
(default 2). Two bounds: Python allocations during delivery (tracemalloc), and the whole
process's peak resident memory (the plan's target: under 256 MB, so a 512 MB machine has room).
This test is what found the uploader's buffer churning a gigabyte of RSS on a 1 GiB pull.
"""

import base64
import json
import os
import resource
import sys
import threading
import tracemalloc
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from pydantic import SecretStr

from patient_access.core.client import FhirClient
from patient_access.core.documents.budget import Budget
from patient_access.core.documents.candidates import Candidate, blob_id
from patient_access.core.documents.downloader import Stored, fetch_one
from patient_access.core.documents.run import record
from patient_access.core.models import DocumentSource, OutcomeStatus
from patient_access.core.settings import BundleSettings, DocumentCaps
from patient_access.destinations.s3 import S3Sink
from patient_access.formatters import render_bundle
from patient_access.spool.local_disk import LocalDiskSpool

from .helpers import NOW, Clock, job, settings, source_settings

MiB = 1024 * 1024
RSS_LIMIT = 256 * MiB
EACH = 256 * MiB
BINARY_JSON = 16 * MiB  # the default cap for a Binary-wrapped answer
pytestmark = pytest.mark.skipif(os.environ.get("PATIENT_ACCESS_SLOW") != "1", reason="set PATIENT_ACCESS_SLOW=1")


class CountingS3:
    """Discards every part, keeps the byte count."""

    def __init__(self):
        self.bytes = 0

    def create_multipart_upload(self, **kw):
        return {"UploadId": "u"}

    def upload_part(self, **kw):
        self.bytes += len(kw["Body"])
        return {"ETag": "e"}

    def complete_multipart_upload(self, **kw):
        return {"ETag": "final"}

    def abort_multipart_upload(self, **kw):
        return {}


class Progress:
    def started(self, upload):
        pass

    def ended(self, upload):
        pass


BLOCK = os.urandom(MiB)
WRAPPED = json.dumps(
    {"resourceType": "Binary", "contentType": "application/pdf", "data": base64.b64encode(BLOCK * 11).decode()}
).encode()


class Documents(BaseHTTPRequestHandler):
    """`/raw/<n>`: EACH bytes, streamed a MiB at a time; `/wrapped`: a Binary JSON near its cap."""

    def do_GET(self):  # noqa: N802 - the stdlib's name
        if self.path == "/fhir/wrapped":
            self.send_response(200)
            self.send_header("Content-Type", "application/fhir+json")
            self.send_header("Content-Length", str(len(WRAPPED)))
            self.end_headers()
            self.wfile.write(WRAPPED)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Length", str(EACH))
        self.end_headers()
        for _ in range(EACH // MiB):
            self.wfile.write(BLOCK)

    def log_message(self, *args):
        pass


def test_a_multi_gigabyte_pull_is_downloaded_and_delivered_in_flat_memory(tmp_path):
    assert len(WRAPPED) < BINARY_JSON
    total = int(float(os.environ.get("PATIENT_ACCESS_SLOW_GB", "2")) * 1024 * MiB)
    n_docs = max(1, total // EACH)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Documents)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}/fhir"
    client = FhirClient(base, SecretStr("tok"), source_settings().fetch, None, Clock())
    caps = DocumentCaps()
    spool = LocalDiskSpool(tmp_path).open("big", bytes(range(32)))
    j = job()
    budget = Budget(caps=caps)
    tracemalloc.start()
    try:
        for i, path in [*((i, f"raw/{i}") for i in range(n_docs)), (n_docs, "wrapped")]:
            spool.append(
                "DocumentReference",
                {"resourceType": "DocumentReference", "id": f"d{i}", "content": [{"attachment": {"url": path}}]},
            )
            cand = Candidate(
                blob_id=blob_id("DocumentReference", f"d{i}", 0),
                source=DocumentSource.DOCUMENT_REFERENCE,
                referenced_by=f"DocumentReference/d{i}",
                attachment_index=0,
                content_type="application/pdf",
                url=path,
            )
            result = fetch_one(client, spool, cand, budget, caps)
            assert isinstance(result, Stored), result
            j.documents.record(record(cand, result))
            budget = budget.spend(result.meta.size)
        spool.flush("DocumentReference")
        j.documents.status = OutcomeStatus.OK
        s3 = CountingS3()
        sink = S3Sink(lambda s: s3)
        receipt = sink.put(j, render_bundle(j, spool, BundleSettings(), NOW), settings().s3, Progress())
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
        server.shutdown()

    assert s3.bytes == receipt.bytes > n_docs * EACH * 4 // 3  # base64 of every document went out
    max_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == "darwin" else 1024)
    assert peak < 2 * sink.part_size + 3 * BINARY_JSON + 32 * MiB, f"python peak {peak / MiB:.0f} MiB"
    assert max_rss < RSS_LIMIT, f"resident peak {max_rss / MiB:.0f} MiB"
    print(
        json.dumps(
            {
                "documents_gib": round(n_docs * EACH / 1024 / MiB, 2),
                "python_peak_mib": round(peak / MiB, 1),
                "max_rss_mib": round(max_rss / MiB),
            }
        )
    )
