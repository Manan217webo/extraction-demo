"""Write-once, content-addressed storage for original source files.

A file is stored once under its SHA-256 (DATA_DIR/blobs/ab/cd/<sha256>) and never
rewritten: the original a value was read from stays exactly as it was received.
Reading re-checks the hash, so a corrupted or altered original is detected
rather than shown. Put DATA_DIR on an encrypted, backed-up volume.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
from pathlib import Path


class BlobCorrupted(RuntimeError):
    """The stored bytes no longer match the hash they were stored under."""


def data_dir() -> Path:
    raw = (os.getenv("DATA_DIR") or "").strip()
    path = Path(raw) if raw else Path(__file__).resolve().parent / "data"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _path(sha256: str) -> Path:
    if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
        raise ValueError("not a sha256")
    return data_dir() / "blobs" / sha256[:2] / sha256[2:4] / sha256


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _put(data: bytes) -> tuple[str, str]:
    digest = sha256_of(data)
    target = _path(digest)
    if target.exists():
        return digest, str(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".incoming-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o444)
        if target.exists():  # another worker stored it first
            os.unlink(tmp)
        else:
            os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return digest, str(target)


async def put(data: bytes) -> tuple[str, str]:
    """Store bytes; returns (sha256, path). Storing the same bytes twice is a no-op."""
    return await asyncio.to_thread(_put, data)


def _get(sha256: str) -> bytes:
    data = _path(sha256).read_bytes()
    if sha256_of(data) != sha256:
        raise BlobCorrupted(f"stored file {sha256[:12]}… no longer matches its hash")
    return data


async def get(sha256: str) -> bytes:
    return await asyncio.to_thread(_get, sha256)


def exists(sha256: str) -> bool:
    return _path(sha256).exists()
