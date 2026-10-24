"""Tests for deploy/backup.sh with a stub rsync (no real copying, no network)."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "backup.sh"
BASH = os.environ.get("HEADWAY_TEST_BASH") or shutil.which("bash")

pytestmark = pytest.mark.skipif(
    BASH is None or (sys.platform == "win32" and "HEADWAY_TEST_BASH" not in os.environ),
    reason="needs a POSIX bash (set HEADWAY_TEST_BASH on Windows)",
)


def _run(tmp_path: Path, rsync_exit: int, alert_pct: int) -> tuple[int, str, str]:
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    calls = tmp_path / "rsync_calls.txt"
    stub = stub_dir / "rsync"
    stub.write_text(f'#!/usr/bin/env bash\necho "$@" >> "{calls.as_posix()}"\nexit {rsync_exit}\n')
    stub.chmod(0o755)
    data = tmp_path / "data"
    (data / "raw").mkdir(parents=True)
    env = {
        **os.environ,
        "PATH": f"{stub_dir}{os.pathsep}{os.environ['PATH']}",
        "BACKUP_DEST": str(tmp_path / "dest"),
        "DATA_DIR": str(data),
        "DISK_ALERT_PCT": str(alert_pct),
        "NTFY_URL": "",
    }
    assert BASH is not None
    result = subprocess.run(
        [BASH, str(SCRIPT)], env=env, capture_output=True, text=True, check=False
    )
    return result.returncode, result.stdout, calls.read_text() if calls.exists() else ""


def test_success(tmp_path: Path) -> None:
    code, out, calls = _run(tmp_path, rsync_exit=0, alert_pct=100)
    assert code == 0, out
    assert "--exclude *.tmp" in calls
    assert "--delete" not in calls
    assert "backup of raw to" in out


def test_rsync_failure_is_reported(tmp_path: Path) -> None:
    code, out, _ = _run(tmp_path, rsync_exit=23, alert_pct=100)
    assert code == 1
    assert "rsync of raw failed with exit code 23" in out


def test_disk_usage_alert(tmp_path: Path) -> None:
    code, out, calls = _run(tmp_path, rsync_exit=0, alert_pct=0)
    assert code == 1
    assert "exceeds 0%" in out
    assert calls  # the backup still runs when the disk is full-ish
