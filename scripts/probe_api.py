"""Probe the Golemio vehicle-positions API and record what it actually returns.

This script is run manually by a person holding a Golemio API key. It is never run in
tests or CI. The key is read only from the ``GOLEMIO_TOKEN`` environment variable and is
sent only in the ``X-Access-Token`` request header; it is never printed or written.

What it does:

1. Polls the JSON vehicle-positions endpoint ``--polls`` times, ``--interval`` seconds
   apart, and saves every response body byte-for-byte together with a metadata file
   (status, latency, response headers with anything auth-, cookie- or IP-related removed).
2. Requests each GTFS-Realtime endpoint once and records only the HTTP status code.
3. Prints and saves a summary of the observed fields: JSON types, occurrence and null
   counts, and the number of records per poll.

The default URLs and parameters follow the official OpenAPI specification
(https://api.golemio.cz/docs/static/vp-output-gateway/openapi.json, rendered at
https://api.golemio.cz/pid/docs/openapi/), checked on 2026-10-24. ``limit`` defaults to 100
there, which truncates the fleet, so the probe requests the documented maximum of 10000.

Usage::

    uv run python scripts/probe_api.py --out probe_out/run2
    uv run python scripts/probe_api.py --out probe_out/public --no-default-params
        --url https://api.golemio.cz/v2/public/vehiclepositions    (one line)
"""

import argparse
import json
import sys
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from headway.config import golemio_token

# Defaults taken from the OpenAPI specification: see the module docstring.
DEFAULT_JSON_URL = "https://api.golemio.cz/v2/vehiclepositions"
DEFAULT_JSON_PARAMS = {"limit": "10000"}
DEFAULT_GTFSRT_URLS = (
    "https://api.golemio.cz/v2/vehiclepositions/gtfsrt/vehicle_positions.pb",
    "https://api.golemio.cz/v2/vehiclepositions/gtfsrt/trip_updates.pb",
    "https://api.golemio.cz/v2/vehiclepositions/gtfsrt/alerts.pb",
    "https://api.golemio.cz/v2/vehiclepositions/gtfsrt/pid_feed.pb",
    "https://api.golemio.cz/v2/vehiclepositions/gtfsrt/pid_display_alerts.pb",
)

AUTH_HEADER = "X-Access-Token"
# Response headers whose name contains any of these parts are dropped before saving.
SENSITIVE_HEADER_PARTS = (
    "auth",
    "token",
    "cookie",
    "session",
    "key",
    "forwarded",
    "ip",
    "via",
)
RATE_LIMIT_HEADER_PARTS = ("ratelimit", "rate-limit", "retry-after")


@dataclass
class FieldStats:
    """Observations of one JSON path across all records of all polls."""

    occurrences: int = 0
    nulls: int = 0
    types: set[str] = field(default_factory=set)


def json_type(value: Any) -> str:
    """Return the JSON type name of a decoded JSON value."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def extract_records(body: Any) -> list[Any]:
    """Return the list of records in a response body.

    A GeoJSON FeatureCollection yields its features, a top-level array yields its items,
    and anything else is treated as a single record.
    """
    if isinstance(body, dict) and isinstance(body.get("features"), list):
        return list(body["features"])
    if isinstance(body, list):
        return list(body)
    return [body]


def _walk(value: Any, path: str, stats: dict[str, FieldStats]) -> None:
    entry = stats.setdefault(path, FieldStats())
    entry.occurrences += 1
    entry.types.add(json_type(value))
    if value is None:
        entry.nulls += 1
    elif isinstance(value, dict):
        for key, child in value.items():
            _walk(child, f"{path}.{key}", stats)
    elif isinstance(value, list):
        for item in value:
            _walk(item, f"{path}[]", stats)


def collect_field_stats(records: Iterable[Any], stats: dict[str, FieldStats]) -> None:
    """Accumulate per-path statistics for the given records into ``stats``.

    Paths are dotted, relative to the record; ``[]`` marks array elements.
    """
    for record in records:
        if isinstance(record, dict):
            for key, child in record.items():
                _walk(child, key, stats)
        else:
            _walk(record, "<record>", stats)


def filter_headers(headers: Mapping[str, str]) -> tuple[dict[str, str], list[str]]:
    """Split response headers into those safe to store and the names of dropped ones."""
    kept: dict[str, str] = {}
    dropped: list[str] = []
    for name, value in headers.items():
        lowered = name.lower()
        if any(part in lowered for part in SENSITIVE_HEADER_PARTS):
            dropped.append(name)
        else:
            kept[name] = value
    return kept, sorted(dropped)


def rate_limit_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Return the headers that look related to rate limiting."""
    return {
        name: value
        for name, value in headers.items()
        if any(part in name.lower() for part in RATE_LIMIT_HEADER_PARTS)
    }


def summarise(stats: Mapping[str, FieldStats], records_per_poll: Sequence[int]) -> dict[str, Any]:
    """Build a JSON-serialisable summary of the observed fields."""
    total_records = sum(records_per_poll)
    fields = []
    for path in sorted(stats):
        entry = stats[path]
        row: dict[str, Any] = {
            "path": path,
            "types": sorted(entry.types),
            "occurrences": entry.occurrences,
            "nulls": entry.nulls,
            "null_rate": round(entry.nulls / entry.occurrences, 4),
        }
        if "[]" not in path:
            row["missing_in_records"] = total_records - entry.occurrences
        fields.append(row)
    return {
        "records_per_poll": list(records_per_poll),
        "total_records": total_records,
        "fields": fields,
    }


def format_summary(summary: Mapping[str, Any]) -> str:
    """Render the summary as a plain-text table."""
    lines = [
        f"records per poll: {summary['records_per_poll']}",
        f"total records:    {summary['total_records']}",
        "",
        f"{'path':<60} {'types':<20} {'occur':>8} {'nulls':>8} {'null%':>7} {'missing':>8}",
    ]
    for row in summary["fields"]:
        missing = row.get("missing_in_records", "")
        lines.append(
            f"{row['path']:<60} {','.join(row['types']):<20} {row['occurrences']:>8} "
            f"{row['nulls']:>8} {row['null_rate'] * 100:>6.1f}% {missing!s:>8}"
        )
    return "\n".join(lines)


def _parse_params(raw: Sequence[str]) -> dict[str, str]:
    params: dict[str, str] = {}
    for item in raw:
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise SystemExit(f"--param expects KEY=VALUE, got {item!r}")
        params[key] = value
    return params


def _timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def run(args: argparse.Namespace) -> int:
    """Run the probe; return the process exit code."""
    token = golemio_token()
    if token is None:
        print("GOLEMIO_TOKEN is not set; aborting.", file=sys.stderr)
        return 1

    out: Path = args.out
    if out.exists() and any(out.iterdir()):
        print(f"{out} is not empty; choose another --out or empty it.", file=sys.stderr)
        return 1
    out.mkdir(parents=True, exist_ok=True)

    defaults = {} if args.no_default_params else DEFAULT_JSON_PARAMS
    params = defaults | _parse_params(args.param)
    headers = {AUTH_HEADER: token.get_secret_value()}
    stats: dict[str, FieldStats] = {}
    records_per_poll: list[int] = []
    rate_limits: list[dict[str, str]] = []

    with httpx.Client(timeout=args.timeout, follow_redirects=False) as client:
        for i in range(1, args.polls + 1):
            if i > 1:
                time.sleep(args.interval)
            fetched_at = datetime.now(UTC)
            stem = f"poll_{i:02d}_{fetched_at:%Y%m%dT%H%M%SZ}"
            meta: dict[str, Any] = {
                "url": args.url,
                "params": params,
                "fetched_at": fetched_at.isoformat(),
            }
            try:
                response = client.get(args.url, params=params, headers=headers)
            except httpx.HTTPError as exc:
                meta["error"] = f"{type(exc).__name__}: {exc}"
                _write_json(out / f"{stem}.meta.json", meta)
                print(f"poll {i}: request failed: {meta['error']}", file=sys.stderr)
                continue

            kept, dropped = filter_headers(response.headers)
            meta |= {
                "status": response.status_code,
                "elapsed_ms": round(response.elapsed.total_seconds() * 1000, 1),
                "bytes": len(response.content),
                "headers": kept,
                "dropped_header_names": dropped,
            }
            (out / f"{stem}.body").write_bytes(response.content)
            rate_limits.append(rate_limit_headers(kept))

            try:
                body = response.json()
            except ValueError:
                meta["json_error"] = "body is not valid JSON"
                body = None
            _write_json(out / f"{stem}.meta.json", meta)

            if body is not None and response.is_success:
                records = extract_records(body)
                records_per_poll.append(len(records))
                collect_field_stats(records, stats)
                if isinstance(body, dict):
                    meta_keys = {k: json_type(v) for k, v in body.items() if k != "features"}
                    print(f"poll {i}: top-level keys besides features: {meta_keys}")
            print(f"poll {i}: status {response.status_code}, {len(response.content)} bytes")

        gtfsrt = []
        for url in args.gtfsrt_url or DEFAULT_GTFSRT_URLS:
            entry: dict[str, Any] = {"url": url}
            try:
                entry["status"] = client.get(url, headers=headers).status_code
            except httpx.HTTPError as exc:
                entry["error"] = f"{type(exc).__name__}: {exc}"
            gtfsrt.append(entry)
            print(f"gtfs-rt {url}: {entry.get('status', entry.get('error'))}")
        _write_json(out / "gtfsrt_status.json", gtfsrt)

    summary = summarise(stats, records_per_poll)
    summary["rate_limit_headers_per_poll"] = rate_limits
    _write_json(out / "summary.json", summary)
    text = format_summary(summary)
    (out / "summary.txt").write_text(text + "\n", encoding="utf-8")
    print()
    print(text)
    if len(set(records_per_poll)) == 1 and records_per_poll[0] and records_per_poll[0] % 100 == 0:
        print(
            "\nNote: every poll returned the same round number of records; the endpoint "
            "may paginate or cap results. Check the documentation for limit/offset."
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--url", default=DEFAULT_JSON_URL, help="JSON vehicle-positions URL.")
    parser.add_argument(
        "--param", action="append", default=[], metavar="KEY=VALUE", help="Query parameter."
    )
    parser.add_argument(
        "--no-default-params",
        action="store_true",
        help=f"Do not send the default query parameters {DEFAULT_JSON_PARAMS}.",
    )
    parser.add_argument("--polls", type=int, default=3, help="Number of JSON polls.")
    parser.add_argument("--interval", type=float, default=30.0, help="Seconds between polls.")
    parser.add_argument("--timeout", type=float, default=30.0, help="Request timeout, seconds.")
    parser.add_argument(
        "--gtfsrt-url", action="append", default=[], help="GTFS-RT URL (replaces defaults)."
    )
    parser.add_argument("--out", type=Path, default=Path("probe_out"), help="Output folder.")
    return parser


def main() -> int:
    """Entry point."""
    return run(build_parser().parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
