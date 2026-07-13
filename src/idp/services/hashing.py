"""Streaming hash helpers for files and small generated payloads."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> tuple[str, int]:
    """Hash a file without reading the full artifact into memory."""
    digest = sha256()
    size_bytes = 0
    with path.open("rb") as source:
        while chunk := source.read(chunk_size):
            digest.update(chunk)
            size_bytes += len(chunk)
    return digest.hexdigest(), size_bytes


def sha256_bytes(payload: bytes) -> str:
    """Return the canonical lower-case SHA-256 for an in-memory payload."""
    return sha256(payload).hexdigest()
