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
