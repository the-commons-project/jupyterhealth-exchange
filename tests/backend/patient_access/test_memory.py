"""Peak memory of the whole delivery path at multi-gigabyte size: documents streamed into the
encrypted spool, rendered as a bundle with base64 inline, digested and uploaded in parts.

Opt-in (it writes and reads gigabytes): PATIENT_ACCESS_SLOW=1, size in PATIENT_ACCESS_SLOW_GB
(default 2). Two bounds: Python allocations during delivery (tracemalloc), and the whole
process's peak resident memory (the plan's target: under 256 MB, so a 512 MB machine has room).
This test is what found the uploader's buffer churning a gigabyte of RSS on a 1 GiB pull.
"""

import json
import os
import resource
import sys
import tracemalloc

import pytest

from patient_access.core.models import OutcomeStatus
from patient_access.core.settings import BundleSettings
from patient_access.destinations.multipart import MultipartUploader
from patient_access.formatters import render_bundle
from patient_access.spool.local_disk import LocalDiskSpool

from .helpers import NOW, job

MiB = 1024 * 1024
RSS_LIMIT = 256 * MiB
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


def test_a_multi_gigabyte_pull_is_delivered_in_flat_memory(tmp_path):
    total = int(float(os.environ.get("PATIENT_ACCESS_SLOW_GB", "2")) * 1024 * MiB)
    each = 256 * MiB
    spool = LocalDiskSpool(tmp_path).open("big", bytes(range(32)))
    block = os.urandom(MiB)
    n_docs = max(1, total // each)
    for i in range(n_docs):
        spool.append(
            "DocumentReference",
            {"resourceType": "DocumentReference", "id": f"d{i}", "content": [{"attachment": {"url": f"Binary/{i}"}}]},
        )
        w = spool.open_blob(f"DocumentReference-d{i}-0")
        for _ in range(each // MiB):
            w.write(block)
        w.commit(content_type="application/pdf", referenced_by=f"DocumentReference/d{i}", source_url=None)
    spool.flush("DocumentReference")
    j = job()
    j.documents.status = OutcomeStatus.OK

    s3 = CountingS3()
    tracemalloc.start()
    uploader = MultipartUploader(s3, bucket="b", key="k", content_type="application/fhir+json", sse={})
    uploader.start()
    uploader.upload(render_bundle(j, spool, BundleSettings(), NOW))
    uploader.complete()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert s3.bytes > n_docs * each * 4 // 3  # base64 of every document went out
    max_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == "darwin" else 1024)
    assert peak < 2 * uploader.part_size + 32 * MiB, f"python peak {peak / MiB:.0f} MiB"
    assert max_rss < RSS_LIMIT, f"resident peak {max_rss / MiB:.0f} MiB"
    print(
        json.dumps(
            {
                "documents_gib": n_docs * each / 1024 / MiB,
                "python_peak_mib": round(peak / MiB, 1),
                "max_rss_mib": round(max_rss / MiB),
            }
        )
    )
