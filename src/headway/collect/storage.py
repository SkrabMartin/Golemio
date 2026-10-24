"""Naming and deterministic compression of raw response bodies."""

import gzip
from datetime import datetime
from pathlib import Path

from headway.fileio import remove_stale_tmp, write_atomic

__all__ = ["compress", "raw_relpath", "remove_stale_tmp", "write_atomic"]


def raw_relpath(fetched_at: datetime, sha256: str) -> Path:
    """Return ``YYYY/MM/DD/HHMMSS_<sha12>.json.gz`` for a poll (``fetched_at`` in UTC)."""
    return Path(f"{fetched_at:%Y/%m/%d/%H%M%S}_{sha256[:12]}.json.gz")


def compress(body: bytes) -> bytes:
    """Gzip a body deterministically (no timestamp), so equal bodies give equal files."""
    return gzip.compress(body, compresslevel=9, mtime=0)
