"""LocalDiskSpool: one directory per job on the worker's disk, every byte encrypted with the job's key.

Layout under {root}/{ref}/:
    {Type}.ndjson       one Fernet token per line, each the JSON of one resource (append and
                        per-line durability; a crash loses at most the line being written)
    blobs/{id}.cs       a document, Cobblestone streaming AEAD: written and read in chunks, so
                        memory stays flat at any size; a truncated file fails authentication
    blobs/{id}.tmp      a document being written; renamed on commit, deleted on abort
    blobs.ndjson        one Fernet token per committed document, {id, meta}, appended on each
                        commit (the last line for an id wins); never rewritten, so committing the
                        ten-thousandth document costs what the first did

Blob ids are hashes of the resource ids (`documents.candidates.blob_id`), so no EHR identifier is
in a file name. The resource key and the blob key are both derived from the job key (HKDF, with
distinct `info`), and every blob is bound to its own name as the Cobblestone context, so a file
moved between blobs or jobs does not decrypt. Without the job's key, held encrypted on the job
row and cleared at a terminal state, a leftover directory is unreadable; `delete` and `sweep`
remove the bytes too.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import time
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import BinaryIO

from cryptography.cobblestone import Cobblestone128Decryptor, Cobblestone128Encryptor
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from patient_access.core.models import BlobMeta, JsonObject, SpoolIndex

CHUNK = 64 * 1024
_SAFE = re.compile(r"[^A-Za-z0-9._-]")
_BLOB_KEY_INFO = b"patient-access/spool/blob"
_RESOURCE_KEY_INFO = b"patient-access/spool/resources"
_INDEX = "blobs.ndjson"


def safe(name: str) -> str:
    out = _SAFE.sub("_", name)[:200]
    return out if out.strip(".") else "_"


def _derive(job_key: bytes, info: bytes, length: int) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=None, info=info).derive(job_key)


class CobblestoneBlobWriter:
    def __init__(self, handle: LocalDiskSpoolHandle, blob_id: str) -> None:
        self._handle = handle
        self.blob_id = blob_id
        self._tmp = handle.blob_path(blob_id).with_suffix(".tmp")
        self._file: BinaryIO | None = open(self._tmp, "wb")  # noqa: SIM115 - closed by commit or abort
        self._enc = Cobblestone128Encryptor(handle.blob_key, handle.context(blob_id))
        self._hash = hashlib.sha256()
        self._size = 0

    @property
    def size(self) -> int:
        return self._size

    def _open(self) -> BinaryIO:
        if self._file is None:
            raise ValueError(f"blob {self.blob_id} already committed or aborted")
        return self._file

    def write(self, chunk: bytes) -> None:
        self._open().write(self._enc.update(chunk))
        self._hash.update(chunk)
        self._size += len(chunk)

    def commit(self, *, content_type: str, referenced_by: str, source_url: str | None) -> BlobMeta:
        f = self._open()
        f.write(self._enc.finalize())
        f.flush()
        os.fsync(f.fileno())
        f.close()
        self._file = None
        os.replace(self._tmp, self._handle.blob_path(self.blob_id))
        meta = BlobMeta(
            content_type=content_type,
            size=self._size,
            sha256=self._hash.hexdigest(),
            referenced_by=referenced_by,
            source_url=source_url,
        )
        self._handle.commit_meta(self.blob_id, meta)
        return meta

    def abort(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None
        self._tmp.unlink(missing_ok=True)


class LocalDiskSpoolHandle:
    def __init__(self, ref: str, path: Path, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError("the spool key must be 32 bytes")
        self.ref = ref
        self.path = path
        self._fernet = Fernet(base64.urlsafe_b64encode(_derive(key, _RESOURCE_KEY_INFO, 32)))
        self.blob_key = _derive(key, _BLOB_KEY_INFO, 16)
        (path / "blobs").mkdir(parents=True, exist_ok=True)
        self._files: dict[str, BinaryIO] = {}
        self._blob_meta: dict[str, BlobMeta] = self._load_meta()

    # --- resources

    def _type_path(self, resource_type: str) -> Path:
        return self.path / f"{safe(resource_type)}.ndjson"

    def append(self, resource_type: str, resource: JsonObject) -> int:
        f = self._files.get(resource_type)
        if f is None:
            f = self._files[resource_type] = open(self._type_path(resource_type), "ab")  # noqa: SIM115
        line = self._fernet.encrypt(json.dumps(resource, separators=(",", ":")).encode()) + b"\n"
        f.write(line)
        return len(line)

    def flush(self, resource_type: str) -> None:
        f = self._files.get(resource_type)
        if f is not None:
            f.flush()
            os.fsync(f.fileno())

    def reset_type(self, resource_type: str) -> None:
        f = self._files.pop(resource_type, None)
        if f is not None:
            f.close()
        self._type_path(resource_type).unlink(missing_ok=True)

    def iter_type(self, resource_type: str) -> Iterator[JsonObject]:
        f = self._files.get(resource_type)
        if f is not None:
            f.flush()
        p = self._type_path(resource_type)
        if not p.exists():
            return
        with open(p, "rb") as fh:
            for line in fh:
                if line.strip():
                    resource: JsonObject = json.loads(self._fernet.decrypt(line.strip()))
                    yield resource

    # --- blobs

    def context(self, blob_id: str) -> bytes:
        return f"{self.ref}/{blob_id}".encode()

    def blob_path(self, blob_id: str) -> Path:
        return self.path / "blobs" / f"{safe(blob_id)}.cs"

    def _load_meta(self) -> dict[str, BlobMeta]:
        p = self.path / _INDEX
        if not p.exists():
            return {}
        out: dict[str, BlobMeta] = {}
        with open(p, "rb") as fh:
            for line in fh:
                if line.strip():
                    entry = json.loads(self._fernet.decrypt(line.strip()))
                    out[entry["id"]] = BlobMeta.model_validate(entry["meta"])
        return out

    def commit_meta(self, blob_id: str, meta: BlobMeta) -> None:
        self._blob_meta[blob_id] = meta
        line = json.dumps({"id": blob_id, "meta": meta.model_dump(mode="json")}).encode()
        with open(self.path / _INDEX, "ab") as f:
            f.write(self._fernet.encrypt(line) + b"\n")
            f.flush()
            os.fsync(f.fileno())

    def blob_meta(self, blob_id: str) -> BlobMeta | None:
        """The committed document's metadata, as of now, or None."""
        meta = self._blob_meta.get(blob_id)
        return meta if meta is not None and self.blob_path(blob_id).exists() else None

    def open_blob(self, blob_id: str) -> CobblestoneBlobWriter:
        return CobblestoneBlobWriter(self, blob_id)

    def has_blob(self, blob_id: str) -> bool:
        return self.blob_meta(blob_id) is not None

    def iter_blob(self, blob_id: str) -> Iterator[bytes]:
        if blob_id not in self._blob_meta:
            raise KeyError(blob_id)
        dec = Cobblestone128Decryptor(self.blob_key, self.context(blob_id))
        with open(self.blob_path(blob_id), "rb") as fh:
            while chunk := fh.read(CHUNK):
                if out := dec.update(chunk):
                    yield out
        if tail := dec.finalize():
            yield tail

    # --- index

    def index(self) -> SpoolIndex:
        for f in self._files.values():
            f.flush()
        types: dict[str, int] = {}
        for p in sorted(self.path.glob("*.ndjson")):
            with open(p, "rb") as fh:
                types[p.stem] = sum(1 for line in fh if line.strip())
        total = sum(p.stat().st_size for p in self.path.rglob("*") if p.is_file())
        held = {k: v for k, v in self._blob_meta.items() if self.blob_path(k).exists()}
        return SpoolIndex(types=types, blobs=held, bytes=total)

    def close(self) -> None:
        for f in self._files.values():
            f.close()
        self._files.clear()


class LocalDiskSpool:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)  # created on first open, so a host that never pulls never writes here

    def open(self, ref: str, key: bytes) -> LocalDiskSpoolHandle:
        return LocalDiskSpoolHandle(safe(ref), self.root / safe(ref), key)

    def delete(self, ref: str) -> None:
        shutil.rmtree(self.root / safe(ref), ignore_errors=True)

    def sweep(self, keep: frozenset[str], older_than: timedelta) -> list[str]:
        if not self.root.exists():
            return []
        cutoff = time.time() - older_than.total_seconds()
        kept = {safe(k) for k in keep}
        gone = []
        for p in self.root.iterdir():
            if p.is_dir() and p.name not in kept and p.stat().st_mtime < cutoff:
                shutil.rmtree(p, ignore_errors=True)
                gone.append(p.name)
        return gone
