"""The encrypted local-disk spool: resources per line under Fernet, documents streamed under
Cobblestone, nothing readable without the job's key."""

import os
import time
from datetime import timedelta

import pytest
from cryptography.exceptions import InvalidTag
from hypothesis import given, settings
from hypothesis import strategies as st

from patient_access.spool.local_disk import LocalDiskSpool

KEY = bytes(range(32))
OTHER_KEY = bytes(range(1, 33))


@pytest.fixture
def spool(tmp_path):
    return LocalDiskSpool(tmp_path / "spool")


def test_resources_round_trip_in_order_and_are_encrypted_on_disk(spool):
    h = spool.open("job-1", KEY)
    h.append("Observation", {"resourceType": "Observation", "id": "o1", "valueString": "SECRET-VALUE"})
    h.append("Observation", {"resourceType": "Observation", "id": "o2"})
    h.flush("Observation")
    assert [r["id"] for r in h.iter_type("Observation")] == ["o1", "o2"]
    assert b"SECRET-VALUE" not in (h.path / "Observation.ndjson").read_bytes()
    assert h.index().types == {"Observation": 2}
    h.reset_type("Observation")
    assert list(h.iter_type("Observation")) == [] and h.index().types == {}


@settings(max_examples=25, deadline=None)
@given(data=st.binary(max_size=200_000), chunk=st.integers(min_value=1, max_value=70_000))
def test_blobs_stream_round_trip_at_any_size(tmp_path_factory, data, chunk):
    h = LocalDiskSpool(tmp_path_factory.mktemp("s")).open("job-1", KEY)
    w = h.open_blob("Doc-1-0")
    for i in range(0, len(data), chunk):
        w.write(data[i : i + chunk])
    meta = w.commit(content_type="text/plain", referenced_by="DocumentReference/1", source_url=None)
    assert meta.size == len(data)
    assert b"".join(h.iter_blob("Doc-1-0")) == data
    assert h.has_blob("Doc-1-0") and "Doc-1-0" in h.index().blobs


def test_an_aborted_blob_leaves_nothing(spool):
    h = spool.open("job-1", KEY)
    w = h.open_blob("Doc-1-0")
    w.write(b"partial")
    w.abort()
    assert not h.has_blob("Doc-1-0")
    assert list((h.path / "blobs").iterdir()) == []


def test_a_truncated_blob_fails_authentication(spool):
    h = spool.open("job-1", KEY)
    w = h.open_blob("Doc-1-0")
    w.write(b"x" * 100_000)
    w.commit(content_type="text/plain", referenced_by="r", source_url=None)
    path = h.blob_path("Doc-1-0")
    path.write_bytes(path.read_bytes()[:40_000])
    with pytest.raises(InvalidTag):
        b"".join(h.iter_blob("Doc-1-0"))


def test_a_blob_moved_to_another_name_or_job_does_not_decrypt(spool):
    h = spool.open("job-1", KEY)
    for name in ("Doc-1-0", "Doc-2-0"):
        w = h.open_blob(name)
        w.write(name.encode() * 1000)
        w.commit(content_type="text/plain", referenced_by="r", source_url=None)
    os.replace(h.blob_path("Doc-1-0"), h.blob_path("Doc-2-0"))
    with pytest.raises(InvalidTag):
        b"".join(h.iter_blob("Doc-2-0"))


def test_metadata_survives_reopening_and_needs_the_key(spool):
    h = spool.open("job-1", KEY)
    w = h.open_blob("Doc-1-0")
    w.write(b"hello")
    w.commit(content_type="text/plain", referenced_by="r", source_url="Binary/1")
    h.close()
    again = spool.open("job-1", KEY)
    assert again.index().blobs["Doc-1-0"].source_url == "Binary/1"
    with pytest.raises(Exception):  # noqa: B017 - Fernet InvalidToken
        spool.open("job-1", OTHER_KEY)


def test_delete_and_sweep(spool):
    spool.open("live", KEY)
    spool.open("orphan", KEY)
    spool.open("recent", KEY)
    old = time.time() - 3600
    for name in ("live", "orphan"):
        os.utime(spool.root / name, (old, old))
    assert spool.sweep(frozenset({"live"}), timedelta(minutes=30)) == ["orphan"]
    spool.delete("live")
    spool.delete("live")  # tolerates a spool that is already gone
    assert sorted(p.name for p in spool.root.iterdir()) == ["recent"]


def test_the_root_is_created_on_first_open(tmp_path):
    spool = LocalDiskSpool(tmp_path / "not-yet")
    assert not (tmp_path / "not-yet").exists()
    assert spool.sweep(frozenset(), timedelta(0)) == []
    spool.open("j", KEY)
    assert (tmp_path / "not-yet" / "j").is_dir()


def test_a_blob_moved_into_another_job_does_not_decrypt(spool):
    """Same key, same name, different job: the job's ref is part of the blob's context."""
    one, two = spool.open("job-1", KEY), spool.open("job-2", KEY)
    for h in (one, two):
        w = h.open_blob("Doc-1-0")
        w.write(h.ref.encode() * 1000)
        w.commit(content_type="text/plain", referenced_by="r", source_url=None)
    os.replace(one.blob_path("Doc-1-0"), two.blob_path("Doc-1-0"))
    with pytest.raises(InvalidTag):
        b"".join(two.iter_blob("Doc-1-0"))


def test_a_blob_read_with_the_wrong_key_does_not_decrypt(tmp_path):
    """Same job, same name, only the key differs."""
    h = LocalDiskSpool(tmp_path / "a").open("job-1", KEY)
    w = h.open_blob("Doc-1-0")
    w.write(b"x" * 10_000)
    meta = w.commit(content_type="text/plain", referenced_by="r", source_url=None)
    stranger = LocalDiskSpool(tmp_path / "b").open("job-1", OTHER_KEY)
    os.replace(h.blob_path("Doc-1-0"), stranger.blob_path("Doc-1-0"))
    stranger.commit_meta("Doc-1-0", meta)
    with pytest.raises(InvalidTag):
        b"".join(stranger.iter_blob("Doc-1-0"))


def test_the_document_index_is_appended_and_the_last_line_wins(spool):
    """Committing never rewrites the index: at the 10,000-document cap that was ~16 GB of writes."""
    h = spool.open("job-1", KEY)
    sizes = []
    for i in range(3):
        w = h.open_blob(f"Doc-{i}-0")
        w.write(b"x" * (i + 1))
        w.commit(content_type="text/plain", referenced_by="r", source_url=None)
        sizes.append((h.path / "blobs" / "index.ndjson").stat().st_size)
    assert sizes[0] < sizes[1] < sizes[2] and sizes[2] - sizes[1] < 1024  # one line each, not the lot
    w = h.open_blob("Doc-0-0")  # the same blob again, as a resumed pass would
    w.write(b"yy")
    w.commit(content_type="text/html", referenced_by="r", source_url=None)
    h.close()
    again = spool.open("job-1", KEY)
    assert again.blob_meta("Doc-0-0").content_type == "text/html" and again.blob_meta("Doc-0-0").size == 2
    assert len(again.index().blobs) == 3


def test_the_index_lists_resource_types_and_nothing_else(spool):
    """The document index is not a type: read as one, its lines went into the bundle as resources."""
    h = spool.open("job-1", KEY)
    h.append("Observation", {"resourceType": "Observation", "id": "o1"})
    w = h.open_blob("Doc-1-0")
    w.write(b"x")
    w.commit(content_type="text/plain", referenced_by="r", source_url=None)
    h.close()
    assert spool.open("job-1", KEY).index().types == {"Observation": 1}


def test_no_resource_id_appears_in_a_file_name(spool):
    from patient_access.core.documents.candidates import blob_id

    h = spool.open("job-1", KEY)
    bid = blob_id("DocumentReference", "eAzqoDIG-5nMnik8YHT6Whw3", 0)
    w = h.open_blob(bid)
    w.write(b"note")
    w.commit(content_type="text/plain", referenced_by="r", source_url=None)
    names = " ".join(p.name for p in h.path.rglob("*"))
    assert "eAzqoDIG" not in names


def test_resources_and_blobs_use_distinct_derived_keys(spool):
    h = spool.open("job-1", KEY)
    assert h.blob_key != KEY[:16] and h._fernet._encryption_key != KEY[16:]
