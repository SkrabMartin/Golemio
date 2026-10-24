"""The Markdown summary of one audited service day, written for people, not for code."""

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from headway.audit.findings import CATEGORIES, Finding

EXAMPLES_PER_CATEGORY = 5


def _pct(share: float | None) -> str:
    return "n/a" if share is None else f"{share * 100:.1f} %"


def _clock(ts: datetime, tz: ZoneInfo, since: datetime | None = None) -> str:
    """Local time of day; if ``ts`` is on a later local day than ``since``, say how many."""
    local = ts.astimezone(tz)
    text = local.strftime("%H:%M:%S")
    if since is not None:
        days = (local.date() - since.astimezone(tz).date()).days
        if days > 0:
            text += f" (+{days} day{'s' if days > 1 else ''})"
    return text


def _span(finding: Finding, tz: ZoneInfo) -> str:
    start, end = _clock(finding.start, tz), _clock(finding.end, tz, finding.start)
    return start if start == end else f"{start}-{end}"


def render_markdown(summary: dict[str, Any], findings: list[Finding], tz_name: str) -> str:
    """Render the summary dict and the findings as a Markdown report."""
    tz = ZoneInfo(tz_name)
    col, pos, th = summary["collection"], summary["positions"], summary["thresholds"]
    lines = [f"# Data-quality audit for service date {summary['service_date']}", ""]
    if summary["synthetic"]:
        lines += [
            "> **SYNTHETIC DATA.** This archive was generated, not collected. The findings "
            "below show that the audit works; they say nothing about real Prague.",
            "",
        ]
    lines += [
        f"All times are local ({tz_name}). Nothing in the data was changed by this audit.",
        "",
    ]

    lines += ["## At a glance", "", "| Question | Answer |", "|---|---|"]
    if col["first"] is not None:
        lines.append(
            f"| Period covered by the collector | {_clock(col['first'], tz)} to "
            f"{_clock(col['last'], tz, col['first'])} |"
        )
    lines += [
        f"| Polls that succeeded | {col['ok_polls']} of {col['expected_polls']} expected "
        f"({_pct(col['coverage'])}) |",
        f"| Position reports (after removing repeats) | {pos['positions']} |",
        f"| Reports that arrived fresh (within {th['positions']['stale_age_s']:.0f} s) | "
        f"{_pct(pos['fresh_share'])} |",
        f"| Reports on their route (within {th['positions']['off_shape_m']:.0f} m) | "
        f"{_pct(pos['on_shape_share'])} |",
        f"| Findings in total | {summary['findings_total']} |",
        "",
    ]

    lines += ["## Findings by category", ""]
    if summary["findings_total"] == 0:
        lines += ["**No findings.** None of the checks below found a problem.", ""]
    lines += ["| Category | Severity | Count | What it means |", "|---|---|---|---|"]
    for category, meaning in CATEGORIES.items():
        info = summary["findings"][category]
        lines.append(f"| {category} | {info['severity']} | {info['count']} | {meaning} |")
    lines.append("")

    for category in CATEGORIES:
        mine = [f for f in findings if f.category == category]
        if not mine:
            continue
        lines += [f"### {category} ({len(mine)})", ""]
        for f in mine[:EXAMPLES_PER_CATEGORY]:
            who = "" if f.entity_type == "poll" else f", {f.entity_type} {f.entity}"
            lines.append(f"- {_span(f, tz)}{who}: {f.details}")
        if len(mine) > EXAMPLES_PER_CATEGORY:
            lines.append(
                f"- ... and {len(mine) - EXAMPLES_PER_CATEGORY} more (see findings.parquet)"
            )
        lines.append("")

    lines += ["## Position delay of the reports", ""]
    if pos["positions"]:
        lines += [
            f"Time between a vehicle sending a position and the collector receiving it: "
            f"median {pos['age_p50_s']:.0f} s, 90 % within {pos['age_p90_s']:.0f} s, "
            f"99 % within {pos['age_p99_s']:.0f} s, worst {pos['age_max_s']:.0f} s.",
            "",
        ]
    else:
        lines += ["No positions were ingested for this date (run `headway ingest` first).", ""]

    lines += ["## Thresholds used", "", "| Setting | Value |", "|---|---|"]
    c, p, t = th["collection"], th["positions"], th["trips"]
    lines += [
        f"| Poll interval | {col['interval_s']:.0f} s |",
        f"| Gap = no successful poll for more than | {c['gap_factor']} x the interval "
        f"(+ {c['gap_tolerance_s']} s for request latency) |",
        f"| Stale feed = identical responses in a row | {c['stale_feed_min_polls']} |",
        f"| API error run = failed polls in a row | {c['api_error_min_ticks']} |",
        f"| Stale position = older than | {p['stale_age_s']} s |",
        f"| Off route = farther from the shape than | {p['off_shape_m']} m |",
        f"| GPS jump = faster than (default) | {p['jump_speed_default_mps']} m/s |",
        f"| GPS jump limits by route type | {p['jump_speed_mps']} |",
        f"| GPS spike = a position farther than this from the line between its neighbours | "
        f"{p['jump_spike_m']} m (neighbours at most {p['jump_spike_span_s']} s apart) |",
        f"| Silent trip = no new position for | {t['silent_min']} min |",
        f"| Two trips of one vehicle overlap by more than | {t['overlap_s']} s |",
        "",
    ]

    lines += ["## Limits of this audit", ""]
    lines += [f"- {note}" for note in summary["notes"]]
    lines.append("")
    return "\n".join(lines)
