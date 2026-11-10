"""One SHA-256 file hasher for the engine's placement, index, street-data and control modules."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path


class FileChangedError(RuntimeError):
    """The file's identity (device, inode, size or times) changed while it was read."""


def _identity(stat: os.stat_result) -> tuple[int, int, int, int, int]:
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def stable_file_sha256(path: Path | str) -> tuple[str, os.stat_result]:
    """Stream one regular file and return its digest and the stat taken before reading.

    Raises FileChangedError when the file is replaced or rewritten during the read.
    """
    path = Path(path)
    before = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    after = path.stat()
    if _identity(after) != _identity(before):
        raise FileChangedError(f"file changed while hashing: {path}")
    return digest.hexdigest(), before


def file_sha256(path: Path | str) -> str:
    """The SHA-256 digest of one file, refusing a file that changes while it is read."""
    return stable_file_sha256(path)[0]
