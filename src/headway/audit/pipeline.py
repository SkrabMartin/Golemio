"""Audit of one service day: findings table, summary JSON and Markdown summary."""

import json
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq

from headway.audit.collection import collection_findings
from headway.audit.config import AuditConfig
from headway.audit.findings import CATEGORIES, FINDINGS_SCHEMA, Finding
from headway.audit.positions import PositionStats, Span, build_timetable, position_findings
from headway.audit.report import render_markdown
from headway.config import Settings
from headway.fileio import write_atomic
from headway.gtfs import GtfsVersionNotFoundError, load_feed, resolve_gtfs
from headway.ingest.pipeline import IngestInputs, day_start_utc, inputs_for, read_manifests


@dataclass(frozen=True)
class AuditInputs:
    """Where the audit reads from and writes to."""

    ingest: IngestInputs
    audit_dir: Path
    interval_s: float
    cfg: AuditConfig


def audit_inputs_for(root: Path, settings: Settings, cfg: AuditConfig) -> AuditInputs:
    """Return the audit inputs of the archive rooted at ``root``."""
    ingest = inputs_for(root, settings)
    return AuditInputs(
        ingest=ingest,
        audit_dir=ingest.positions_dir.parent / "audit",
        interval_s=float(settings.collect.interval_s),
        cfg=cfg,
    )


def _timetable(day: date, df: pl.DataFrame, inputs: AuditInputs) -> tuple[Any, str | None]:
    """Return the timetable of the day, or None and the reason it is missing."""
    try:
        resolved = resolve_gtfs(day, inputs.ingest.gtfs_dir)
    except GtfsVersionNotFoundError:
        return (
            None,
            "No timetable (GTFS) covers this date, so trip ids and routes were not checked.",
        )
    feed = load_feed(resolved, tables=("trips", "shapes"))
    wanted = set(df["trip_id"].unique().to_list())
    return build_timetable(
        feed["trips"], feed["shapes"] if "shapes" in feed.tables else None, wanted
    ), None


def audit_date(day: date, inputs: AuditInputs) -> dict[str, Any]:
    """Audit one service day; write its files and return the summary."""
    ingest = inputs.ingest
    start = day_start_utc(day, ingest.tz, ingest.cfg.service_day_start_min)
    end = day_start_utc(day + timedelta(days=1), ingest.tz, ingest.cfg.service_day_start_min)
    records = read_manifests(ingest.manifest_dir, start, end)
    cfg = inputs.cfg

    findings, col = collection_findings(records, inputs.interval_s, cfg.collection)
    explained: list[Span] = [(f.start, f.end) for f in findings]

    notes = [
        "Delays reported by the API are not audited here; that needs reconstructed stop "
        "events (a later phase).",
        "A silent trip inside a collector problem is not reported as silent, because the "
        "collector problem explains it.",
        "Handover duplicates (one message on two trips) are reported, not removed.",
    ]
    path = ingest.positions_dir / f"service_date={day}" / "part-0.parquet"
    df = pl.read_parquet(path) if path.exists() else pl.DataFrame()
    if df.height == 0:
        notes.insert(0, "No positions were ingested for this date.")
        pos_stats = PositionStats()
    else:
        timetable, missing = _timetable(day, df, inputs)
        if missing:
            notes.insert(0, missing)
        position, pos_stats = position_findings(
            df, cfg.positions, cfg.trips, timetable, explained, col.last_ok
        )
        findings += position
        if pos_stats.trips_without_shape:
            notes.append(
                f"{pos_stats.trips_without_shape} trip(s) have no shape, so they were "
                "not checked against their route."
            )
    findings.sort(key=lambda f: (f.start, f.category, f.entity))

    counts = {c: sum(1 for f in findings if f.category == c) for c in CATEGORIES}
    summary: dict[str, Any] = {
        "service_date": day.isoformat(),
        "synthetic": ingest.synthetic,
        "collection": {
            "interval_s": inputs.interval_s,
            "attempts": col.attempts,
            "ok_polls": col.ok_polls,
            "first": col.first,
            "last": col.last,
            "expected_polls": col.expected_polls,
            "coverage": col.coverage,
            "failed_ticks": col.failed_ticks,
            "gap_seconds": col.gap_seconds,
        },
        "positions": {
            "positions": pos_stats.positions,
            "trips": pos_stats.trips,
            "vehicles": pos_stats.vehicles,
            "no_coordinates": pos_stats.no_coordinates,
            "age_p50_s": pos_stats.age_p50_s,
            "age_p90_s": pos_stats.age_p90_s,
            "age_p99_s": pos_stats.age_p99_s,
            "age_max_s": pos_stats.age_max_s,
            "stale": pos_stats.stale,
            "fresh_share": pos_stats.fresh_share,
            "shape_checked": pos_stats.shape_checked,
            "on_shape_share": pos_stats.on_shape_share,
            "trips_without_shape": pos_stats.trips_without_shape,
            "timetable_available": pos_stats.timetable_available,
        },
        "findings": {c: {"count": counts[c], "severity": cfg.severity[c]} for c in CATEGORIES},
        "findings_total": len(findings),
        "thresholds": cfg.model_dump(),
        "notes": notes,
    }

    table = pa.Table.from_pylist(
        [
            {
                "service_date": day,
                "category": f.category,
                "severity": cfg.severity[f.category],
                "entity_type": f.entity_type,
                "entity": f.entity,
                "start_ts": f.start,
                "end_ts": f.end,
                "value": f.value,
                "details": f.details,
            }
            for f in findings
        ],
        schema=FINDINGS_SCHEMA,
    )
    folder = inputs.audit_dir / f"service_date={day}"
    sink = pa.BufferOutputStream()
    pq.write_table(table, sink, compression="zstd")
    write_atomic(folder / "findings.parquet", sink.getvalue().to_pybytes())
    write_atomic(
        folder / "summary.json",
        (json.dumps(summary, indent=2, sort_keys=True, default=str) + "\n").encode("utf-8"),
    )
    write_atomic(
        folder / "summary.md",
        render_markdown(summary, findings, ingest.tz).encode("utf-8"),
    )
    return summary


def audit_dates(days: list[date], inputs: AuditInputs) -> list[dict[str, Any]]:
    """Audit the given service days in order and return their summaries."""
    return [audit_date(day, inputs) for day in days]


__all__ = ["AuditInputs", "Finding", "audit_date", "audit_dates", "audit_inputs_for"]
