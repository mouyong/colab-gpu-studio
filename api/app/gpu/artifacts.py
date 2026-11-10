"""Owner-bound local object storage; bounded-memory resumable Mock protocol."""

from __future__ import annotations

import hashlib
import json
import os
import re
from functools import wraps
from pathlib import Path

from ..filelocks import file_lock

_ID = re.compile(r"^[a-f0-9]{32}$")


class ArtifactError(ValueError):
    pass


def locked(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        # One storage-volume lock coordinates chunks/finalize across processes.
        lock_path = self.root / ".artifact.lock"
        # O_NOFOLLOW does not exist on Windows; there the storage root is not shared with workers.
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "a+b") as handle:
            with file_lock(handle):
                return method(self, *args, **kwargs)

    return call


class LocalArtifactStore:
    chunk_limit = 8 * 1024 * 1024
    max_size = 2 * 1024**3

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key, suffix):
        if not isinstance(key, str) or not _ID.fullmatch(key):
            raise ArtifactError("invalid opaque object key")
        path = self.root / (key + suffix)
        if path.is_symlink() or path.resolve().parent != self.root:
            raise ArtifactError("object path escaped storage")
        return path

    def _load(self, key, owner, execution):
        value = json.loads(self._path(key, ".json").read_text())
        if value["owner_id"] != owner or value["execution_id"] != execution:
            raise ArtifactError("artifact ownership mismatch")
        return value

    def _save(self, key, value):
        temp = self._path(key, ".tmp")
        with temp.open("w") as handle:
            json.dump(value, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        temp.replace(self._path(key, ".json"))

    @locked
    def begin(self, owner, execution, filename, mime, size_bytes, sha256):
        if not owner or not execution or not isinstance(size_bytes, int) or isinstance(size_bytes, bool):
            raise ArtifactError("invalid artifact identity or size")
        if not 0 < size_bytes <= self.max_size or not re.fullmatch("[a-f0-9]{64}", sha256):
            raise ArtifactError("invalid size or sha256")
        if mime not in {"video/mp4", "video/webm", "image/png", "image/jpeg", "image/webp", "image/gif", "application/json"}:
            raise ArtifactError("unsupported artifact type")
        if Path(filename).name != filename or len(filename) > 140:
            raise ArtifactError("invalid filename")
        key = hashlib.sha256(
            json.dumps([owner, execution, filename], separators=(",", ":")).encode()
        ).hexdigest()[:32]
        value = dict(
            artifact_id=key,
            owner_id=owner,
            execution_id=execution,
            filename=filename,
            mime=mime,
            size_bytes=size_bytes,
            sha256=sha256,
            upload_state="uploading",
            storage={"provider": "local", "object_key": key},
            is_mock=True,
        )
        if self._path(key, ".json").exists():
            existing = self._load(key, owner, execution)
            if any(existing[k] != value[k] for k in ("filename", "mime", "size_bytes", "sha256")):
                raise ArtifactError("artifact idempotency conflict")
            return existing
        # A crash after creating bytes but before metadata is safe to recover.
        self._path(key, ".data").touch(exist_ok=True)
        self._save(key, value)
        return value

    @locked
    def offset(self, key, owner, execution):
        self._load(key, owner, execution)
        return self._path(key, ".data").stat().st_size

    @locked
    def append(self, key, owner, execution, offset, chunk):
        value = self._load(key, owner, execution)
        if not isinstance(chunk, bytes) or not chunk or len(chunk) > self.chunk_limit:
            raise ArtifactError("invalid chunk")
        path = self._path(key, ".data")
        current = path.stat().st_size
        if offset < 0 or offset + len(chunk) > value["size_bytes"]:
            raise ArtifactError("chunk out of bounds")
        with path.open("r+b") as file:
            if offset < current:
                file.seek(offset)
                if file.read(len(chunk)) == chunk:
                    return current
                raise ArtifactError("chunk replay conflict")
            if offset != current or value["upload_state"] == "verified":
                raise ArtifactError("unexpected upload offset")
            file.seek(offset)
            file.write(chunk)
            file.flush()
            __import__("os").fsync(file.fileno())
        return offset + len(chunk)

    @locked
    def verify(self, key, owner, execution):
        value = self._load(key, owner, execution)
        path = self._path(key, ".data")
        sha = hashlib.sha256()
        with path.open("rb") as file:
            while chunk := file.read(self.chunk_limit):
                sha.update(chunk)
        if path.stat().st_size != value["size_bytes"] or sha.hexdigest() != value["sha256"]:
            raise ArtifactError("artifact length or checksum mismatch")
        value["upload_state"] = "verified"
        self._save(key, value)
        return value

    def publish_file(self, owner, execution, source, mime):
        """Stream a worker file into resumable storage; never inline media in JSON."""
        source = Path(source)
        if source.is_symlink() or not source.is_file():
            raise ArtifactError('invalid worker output file')
        size = source.stat().st_size
        if not 0 < size <= self.max_size:
            raise ArtifactError('worker output exceeds storage limit')
        checksum = hashlib.sha256()
        with source.open('rb') as reader:
            while chunk := reader.read(self.chunk_limit):
                checksum.update(chunk)
        item = self.begin(owner, execution, source.name, mime, size, checksum.hexdigest())
        key = item['artifact_id']
        with source.open('rb') as reader:
            reader.seek(self.offset(key, owner, execution))
            while chunk := reader.read(self.chunk_limit):
                # Concurrent replay is checked byte-for-byte by append.
                start = reader.tell() - len(chunk)
                self.append(key, owner, execution, start, chunk)
        return self.verify(key, owner, execution)

    def verify_manifest(self, item, owner, execution):
        key = (item.get("storage") or {}).get("object_key")
        value = self.verify(key, owner, execution)
        if any(item.get(k) != value[k] for k in ("artifact_id", "filename", "mime", "size_bytes", "sha256")):
            raise ArtifactError("untrusted artifact metadata")
        return value, self._path(key, ".data")
