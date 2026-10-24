"""Atomic, deterministic storage of raw response bodies."""

import contextlib
import gzip
import os
from datetime import datetime
from pathlib import Path

TMP_SUFFIX = ".tmp"


def raw_relpath(fetched_at: datetime, sha256: str) -> Path:
    """Return ``YYYY/MM/DD/HHMMSS_<sha12>.json.gz`` for a poll (``fetched_at`` in UTC)."""
    return Path(f"{fetched_at:%Y/%m/%d/%H%M%S}_{sha256[:12]}.json.gz")


def compress(body: bytes) -> bytes:
    """Gzip a body deterministically (no timestamp), so equal bodies give equal files."""
    return gzip.compress(body, compresslevel=9, mtime=0)


def write_atomic(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` so that readers never see a partial file.

    The data goes to a temporary file in the same directory, is flushed to disk and then
    renamed over the target. On any error the temporary file is removed.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}{TMP_SUFFIX}")
    try:
        with tmp.open("wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        raise


def remove_stale_tmp(root: Path) -> list[Path]:
    """Delete temporary files left behind by a killed process; return what was removed."""
    removed = []
    if root.exists():
        for tmp in root.rglob(f"*{TMP_SUFFIX}"):
            tmp.unlink(missing_ok=True)
            removed.append(tmp)
    return removed
