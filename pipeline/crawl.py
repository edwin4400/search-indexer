"""Stage 1 - enumerate a filesystem tree into ItemRef records.

Deliberately dumb and dependency-free: walk, stat, hash. This is a correct
one-shot crawl; incremental change detection lives in `watch.py`.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterator

# Directories never worth indexing, and never worth descending into.
SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".mypy_cache",
    ".pytest_cache", ".venv", "venv", "env", "site-packages",
}

# Names that never hold content worth indexing: office/editor lock files, OS
# metadata, and partially written downloads. Left in, they produce a stream of
# permission-denied failures and pollute results.
IGNORE_NAMES = {"thumbs.db", "desktop.ini", ".ds_store", "ehthumbs.db", "iconcache.db"}
IGNORE_PREFIXES = ("~$",)
IGNORE_SUFFIXES = (".tmp", ".temp", ".crdownload", ".partial", ".part")


def is_ignored(filename: str) -> bool:
    lowered = filename.lower()
    if lowered in IGNORE_NAMES:
        return True
    if lowered.startswith(IGNORE_PREFIXES):
        return True
    return lowered.endswith(IGNORE_SUFFIXES)

# Hash at most this much of a file. Large files still get indexed; they just
# fall back to (size, mtime) for change detection.
HASH_MAX_BYTES = 32 * 1024 * 1024


@dataclass
class ItemRef:
    """One discovered file, before extraction."""

    uri: str
    name: str
    size: int
    mtime: str
    content_hash: str | None = None
    parent_uri: str | None = None
    source: str = "files"
    origin: str | None = None      # what the source needs to reopen this item


def iso_utc(timestamp: float) -> str:
    """Timestamps are stored with sub-second precision deliberately.

    Second-granularity mtimes are not enough for change detection: two saves
    inside the same second that leave the size identical would look unchanged.
    Microseconds are finer than any filesystem's mtime resolution in practice.
    """
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec="microseconds")


def sha256_file(path: str, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def walk(
    root: str,
    compute_hash: bool = True,
    hash_max_bytes: int = HASH_MAX_BYTES,
) -> Iterator[ItemRef]:
    """Yield every regular file under *root*.

    Ordering is deterministic (sorted) so runs are comparable and snapshot
    diffs are meaningful.

    Set *compute_hash* to False for cheap change-detection scans: reading and
    hashing every file is far more expensive than stat-ing it, and the watcher
    only needs (size, mtime) to decide what changed.
    """
    root = os.path.abspath(root)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")
        )
        for filename in sorted(filenames):
            if is_ignored(filename):
                continue
            path = os.path.join(dirpath, filename)
            try:
                stat = os.stat(path)
            except OSError:
                continue
            if not os.path.isfile(path):
                continue
            content_hash = None
            if compute_hash and stat.st_size <= hash_max_bytes:
                try:
                    content_hash = sha256_file(path)
                except OSError:
                    content_hash = None
            yield ItemRef(
                uri=path,
                name=filename,
                size=stat.st_size,
                mtime=iso_utc(stat.st_mtime),
                content_hash=content_hash,
            )
