"""File helpers shared by all layers."""

import contextlib
import os
from pathlib import Path

TMP_SUFFIX = ".tmp"


def tmp_path_for(path: Path) -> Path:
    """Return the temporary sibling used while ``path`` is being written."""
    return path.with_name(f"{path.name}.{os.getpid()}{TMP_SUFFIX}")


def write_atomic(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` so that readers never see a partial file.

    The data goes to a temporary file in the same directory, is flushed to disk and then
    renamed over the target. On any error the temporary file is removed.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = tmp_path_for(path)
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
