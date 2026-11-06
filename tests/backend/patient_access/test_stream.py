"""The framer, the base64 carry and the digest: serializer-grade checks, sizes chosen to straddle
every boundary (the 3-byte group, Cobblestone's chunk, the downloader's chunk)."""

import base64
import hashlib
import json

import ijson
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from patient_access.formatters.stream import BinaryEntry, Digest, ResourceEntry, base64_stream, frame

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


def test_frame_writes_valid_json_with_documents_as_binaries():
    blobs = {"b1": b"note one \xe2\x9c\x93", "b2": bytes(range(256)) * 100}
    entries = [
        ResourceEntry(full_url="urn:uuid:1", resource={"resourceType": "Patient", "name": [{"text": "Zoë"}]}),
        BinaryEntry(full_url="urn:uuid:2", id="b1", content_type="text/plain; charset=utf-8", blob_id="b1", size=12),
        BinaryEntry(full_url="urn:uuid:3", id="b2", content_type="application/pdf", blob_id="b2", size=25600),
    ]
    raw = b"".join(frame({"resourceType": "Bundle", "type": "document"}, entries, lambda b: [blobs[b]]))
    bundle = json.loads(raw)
    assert bundle["type"] == "document" and len(bundle["entry"]) == 3
    assert bundle["entry"][0]["resource"]["name"][0]["text"] == "Zoë"
    first, second = (e["resource"] for e in bundle["entry"][1:])
    assert first == {
        "resourceType": "Binary",
        "id": "b1",
        "contentType": "text/plain; charset=utf-8",
        "data": base64.b64encode(blobs["b1"]).decode(),
    }
    assert base64.b64decode(second["data"]) == blobs["b2"]
    assert list(ijson.parse(raw))  # a streaming parser reads it to the end


def test_an_empty_document_is_a_binary_without_data():
    """FHIR forbids empty strings, so no `"data": ""`."""
    entry = BinaryEntry(full_url="urn:uuid:1", id="b", content_type="text/plain", blob_id="b", size=0)
    bundle = json.loads(b"".join(frame({"resourceType": "Bundle"}, [entry], lambda b: [])))
    assert "data" not in bundle["entry"][0]["resource"]


def test_frame_with_no_entries():
    assert json.loads(b"".join(frame({"resourceType": "Bundle"}, [], lambda b: []))) == {
        "resourceType": "Bundle",
        "entry": [],
    }


def test_the_output_is_ascii_whatever_the_ehr_sent():
    """A lone surrogate (valid JSON escape, invalid Unicode) once failed every delivery attempt."""
    resource = json.loads('{"resourceType":"Observation","valueString":"bad \\ud800 text","note":"Zoë"}')
    raw = b"".join(frame({"resourceType": "Bundle"}, [ResourceEntry("urn:uuid:1", resource)], lambda b: []))
    assert raw.isascii()
    assert json.loads(raw)["entry"][0]["resource"]["note"] == "Zoë"


def test_a_non_finite_number_is_refused_rather_than_written():
    entry = ResourceEntry("urn:uuid:1", {"resourceType": "Observation", "valueQuantity": {"value": float("nan")}})
    with pytest.raises(ValueError):
        b"".join(frame({"resourceType": "Bundle"}, [entry], lambda b: []))


@settings(max_examples=100)
@given(text=st.text())
def test_any_text_in_a_resource_frames_as_valid_json(text):
    entry = ResourceEntry("urn:uuid:1", {"resourceType": "Observation", "valueString": text})
    raw = b"".join(frame({"resourceType": "Bundle"}, [entry], lambda b: []))
    assert json.loads(raw)["entry"][0]["resource"]["valueString"] == text


@settings(max_examples=50)
@given(chunks=st.lists(st.binary(max_size=200), max_size=20))
def test_digest_passes_through_and_hashes(chunks):
    digest = Digest(chunks)
    assert b"".join(digest) == b"".join(chunks)
    assert digest.size == sum(len(c) for c in chunks)
    assert digest.sha256 == hashlib.sha256(b"".join(chunks)).hexdigest()
