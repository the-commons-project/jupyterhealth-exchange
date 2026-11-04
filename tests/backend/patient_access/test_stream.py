"""The framer, the base64 carry and the digest: serializer-grade checks, sizes chosen to straddle
every boundary (the 3-byte group, Cobblestone's chunk, the downloader's chunk)."""

import base64
import hashlib
import json

import ijson
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from patient_access.formatters.stream import Digest, Entry, FramingError, InlineBlob, base64_stream, frame

KiB = 1024


def chunked(data: bytes, sizes: list[int]) -> list[bytes]:
    out, i = [], 0
    for size in sizes:
        out.append(data[i : i + size])
        i += size
    out.append(data[i:])
    return [c for c in out if c] or [b""]


@given(data=st.binary(max_size=4 * KiB), sizes=st.lists(st.integers(min_value=1, max_value=97), max_size=40))
def test_base64_stream_equals_one_shot_encoding(data, sizes):
    assert b"".join(base64_stream(chunked(data, sizes))) == base64.b64encode(data)


@pytest.mark.parametrize("size", [0, 1, 2, 3, 4, 47 * KiB, 48 * KiB, 48 * KiB + 1, 49 * KiB, 64 * KiB + 2])
def test_base64_stream_at_boundary_sizes(size):
    data = bytes(range(256)) * (size // 256) + bytes(size % 256)
    for chunk_size in (1, 3, 16 * KiB, 48 * KiB, 64 * KiB):
        pieces = [data[i : i + chunk_size] for i in range(0, len(data), chunk_size)] or [b""]
        assert b"".join(base64_stream(pieces)) == base64.b64encode(data)


def test_frame_writes_valid_json_with_documents_inline():
    blobs = {"b1": b"note one \xe2\x9c\x93", "b2": bytes(range(256)) * 100}
    entries = [
        Entry(full_url="urn:uuid:1", resource={"resourceType": "Patient", "name": [{"text": "Zoë"}]}),
        Entry(
            full_url="urn:uuid:2",
            resource={
                "resourceType": "DiagnosticReport",
                "presentedForm": [{"contentType": "text/plain", "data": "__ph0__"}, {"data": "__ph1__"}],
            },
            inline=(InlineBlob(placeholder="__ph1__", blob_id="b2"), InlineBlob(placeholder="__ph0__", blob_id="b1")),
        ),
    ]
    raw = b"".join(frame({"resourceType": "Bundle", "type": "document"}, entries, lambda b: [blobs[b]]))
    bundle = json.loads(raw)
    assert bundle["type"] == "document" and len(bundle["entry"]) == 2
    assert bundle["entry"][0]["resource"]["name"][0]["text"] == "Zoë"
    forms = bundle["entry"][1]["resource"]["presentedForm"]
    assert base64.b64decode(forms[0]["data"]) == blobs["b1"]
    assert base64.b64decode(forms[1]["data"]) == blobs["b2"]
    assert list(ijson.parse(raw))  # a streaming parser reads it to the end


def test_frame_with_no_entries():
    assert json.loads(b"".join(frame({"resourceType": "Bundle"}, [], lambda b: []))) == {
        "resourceType": "Bundle",
        "entry": [],
    }


def test_missing_placeholder_is_a_framing_error():
    entry = Entry(full_url="u", resource={"data": "x"}, inline=(InlineBlob(placeholder="__nope__", blob_id="b"),))
    with pytest.raises(FramingError):
        b"".join(frame({"resourceType": "Bundle"}, [entry], lambda b: [b"x"]))


@settings(max_examples=50)
@given(chunks=st.lists(st.binary(max_size=200), max_size=20))
def test_digest_passes_through_and_hashes(chunks):
    digest = Digest(chunks)
    assert b"".join(digest) == b"".join(chunks)
    assert digest.size == sum(len(c) for c in chunks)
    assert digest.sha256 == hashlib.sha256(b"".join(chunks)).hexdigest()
