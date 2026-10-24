"""Offline tests for the pure helpers of scripts/probe_api.py (no network)."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "probe_api.py"


def _load_probe() -> ModuleType:
    spec = importlib.util.spec_from_file_location("probe_api", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["probe_api"] = module
    spec.loader.exec_module(module)
    return module


probe = _load_probe()


def test_filter_headers_drops_sensitive_names() -> None:
    headers = {
        "Content-Type": "application/json",
        "Set-Cookie": "a=b",
        "X-Access-Token": "secret",
        "Authorization": "Bearer x",
        "X-Forwarded-For": "203.0.113.7",
        "X-Real-IP": "203.0.113.7",
        "Via": "1.1 proxy",
        "X-RateLimit-Remaining": "10",
    }
    kept, dropped = probe.filter_headers(headers)
    assert kept == {"Content-Type": "application/json", "X-RateLimit-Remaining": "10"}
    assert "X-Access-Token" in dropped
    assert "Set-Cookie" in dropped


def test_rate_limit_headers() -> None:
    headers = {"Retry-After": "5", "X-RateLimit-Limit": "100", "Content-Length": "3"}
    assert probe.rate_limit_headers(headers) == {"Retry-After": "5", "X-RateLimit-Limit": "100"}


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"type": "FeatureCollection", "features": [1, 2]}, 2),
        ([{"a": 1}, {"a": 2}, {"a": 3}], 3),
        ({"a": 1}, 1),
    ],
)
def test_extract_records(body: object, expected: int) -> None:
    assert len(probe.extract_records(body)) == expected


def test_field_stats_and_summary() -> None:
    records = [
        {"id": "a", "props": {"delay": 30, "note": None}, "coords": [14.4, 50.1]},
        {"id": "b", "props": {"delay": None}, "coords": [14.5, 50.0]},
    ]
    stats: dict[str, Any] = {}
    probe.collect_field_stats(records, stats)
    summary = probe.summarise(stats, [2])
    rows = {row["path"]: row for row in summary["fields"]}

    assert rows["id"]["types"] == ["str"]
    assert rows["props.delay"]["types"] == ["int", "null"]
    assert rows["props.delay"]["null_rate"] == 0.5
    assert rows["props.note"]["missing_in_records"] == 1
    assert rows["coords[]"]["occurrences"] == 4
    assert "missing_in_records" not in rows["coords[]"]
    assert "props.delay" in probe.format_summary(summary)


def test_default_params_request_documented_maximum() -> None:
    assert probe.DEFAULT_JSON_PARAMS == {"limit": "10000"}
    args = probe.build_parser().parse_args(["--no-default-params"])
    assert args.no_default_params is True


def test_missing_token_aborts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("GOLEMIO_TOKEN", raising=False)
    args = probe.build_parser().parse_args(["--out", str(tmp_path / "out")])
    assert probe.run(args) == 1
    assert not (tmp_path / "out").exists()
