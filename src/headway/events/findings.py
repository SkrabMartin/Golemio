"""The write-up of how the reported delay compares with the reconstructed one.

Reads the comparison tables of all reconstructed dates and writes a Markdown page with two
charts into ``<archive>/data/findings/``. On a synthetic archive the planted truth also lets
the page show how accurate the reconstruction is. The page is generated, never edited.
"""

from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
import polars as pl

from headway.events.pipeline import EventsInputs

# The reference palette of the dataviz method: categorical slots 1 and 2 on the light surface
# (validated: colour-vision separation and contrast pass), text in ink tokens.
SURFACE, INK, INK_2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e3e2de"
BLUE, ORANGE = "#2a78d6", "#eb6834"
ROUTE_TYPE_NAMES = {0: "tram", 1: "metro", 2: "rail", 3: "bus", 11: "trolleybus"}


def _style(ax: Any) -> None:
    ax.set_facecolor(SURFACE)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.tick_params(colors=INK_2, length=0, labelsize=9)
    ax.grid(axis="x", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def _spread(frame: pl.DataFrame, by: str) -> pl.DataFrame:
    return (
        frame.group_by(by)
        .agg(
            pl.len().alias("n"),
            pl.col("diff_s").median().alias("median"),
            pl.col("diff_s").quantile(0.1).alias("p10"),
            pl.col("diff_s").quantile(0.9).alias("p90"),
        )
        .sort(by)
    )


def _by_route_chart(frame: pl.DataFrame, path: Path) -> None:
    rows = _spread(frame, "route_id").sort("route_id", descending=True)
    fig, ax = plt.subplots(figsize=(7.2, 0.55 * rows.height + 1.6), facecolor=SURFACE)
    _style(ax)
    labels = [f"{r['route_id']}  (n={r['n']})" for r in rows.iter_rows(named=True)]
    y = np.arange(rows.height)
    ax.hlines(y, rows["p10"].to_numpy(), rows["p90"].to_numpy(), color=BLUE, linewidth=2, alpha=0.5)
    ax.plot(rows["median"].to_numpy(), y, "o", color=BLUE, markersize=7, markeredgecolor=SURFACE)
    ax.axvline(0, color=INK_2, linewidth=1)
    ax.set_yticks(y, labels, color=INK)
    ax.set_xlabel(
        "reported minus reconstructed delay (s)   median, 10th to 90th percentile",
        color=INK_2,
        fontsize=9,
    )
    ax.set_title(
        "Reported delay against reconstructed delay, by route", color=INK, fontsize=11, loc="left"
    )
    fig.tight_layout()
    fig.savefig(path, dpi=150, facecolor=SURFACE)
    plt.close(fig)


def _by_position_chart(frame: pl.DataFrame, path: Path) -> None:
    order = ["first", "middle", "last"]
    types = sorted(frame["route_type"].unique().to_list())[:2]  # two series: the palette's slots
    fig, ax = plt.subplots(figsize=(7.2, 3.2), facecolor=SURFACE)
    _style(ax)
    for k, (route_type, colour) in enumerate(zip(types, (BLUE, ORANGE), strict=False)):
        part = _spread(frame.filter(pl.col("route_type") == route_type), "stop_position")
        by_pos = {r["stop_position"]: r for r in part.iter_rows(named=True)}
        for i, pos in enumerate(order):
            if pos not in by_pos:
                continue
            row, y = by_pos[pos], i + (k - 0.5) * 0.3
            ax.hlines(y, row["p10"], row["p90"], color=colour, linewidth=2, alpha=0.5)
            ax.plot(row["median"], y, "o", color=colour, markersize=7, markeredgecolor=SURFACE)
        name = ROUTE_TYPE_NAMES.get(route_type, str(route_type))
        ax.plot([], [], "o", color=colour, label=name)
    ax.axvline(0, color=INK_2, linewidth=1)
    ax.set_yticks(range(3), [f"{p} stop" for p in order], color=INK)
    ax.invert_yaxis()
    ax.set_xlabel(
        "reported minus reconstructed delay (s)   median, 10th to 90th percentile",
        color=INK_2,
        fontsize=9,
    )
    ax.set_title(
        "Reported delay against reconstructed delay, by stop position",
        color=INK,
        fontsize=11,
        loc="left",
    )
    ax.legend(frameon=False, labelcolor=INK_2, fontsize=9, loc="upper right")
    fig.tight_layout()
    fig.savefig(path, dpi=150, facecolor=SURFACE)
    plt.close(fig)


def _table(frame: pl.DataFrame, by: str, names: dict[Any, str] | None = None) -> list[str]:
    lines = [
        f"| {by.replace('_', ' ')} | n | median | 10th pct | 90th pct |",
        "|---|---|---|---|---|",
    ]
    for r in _spread(frame, by).iter_rows(named=True):
        label = (names or {}).get(r[by], str(r[by]))
        lines.append(
            f"| {label} | {r['n']} | {r['median']:+.1f} | {r['p10']:+.1f} | {r['p90']:+.1f} |"
        )
    return lines


def _accuracy_lines(events: pl.DataFrame, truth: pl.DataFrame) -> list[str]:
    joined = events.join(truth, on=["trip_id", "stop_sequence"])
    lines = [
        "| time | method | n | within stated uncertainty | median error (s) |",
        "|---|---|---|---|---|",
    ]
    for name, time_col, unc_col, truth_col in (
        ("arrival", "arr_ts", "uncert_s", "true_arr"),
        ("departure", "dep_ts", "dep_uncert_s", "true_dep"),
    ):
        for method in ("plateau", "interpolated"):
            sub = joined.filter(
                (pl.col("method") == method)
                & pl.col(time_col).is_not_null()
                & pl.col(unc_col).is_not_null()
            )
            if sub.height == 0:
                continue
            error = np.abs(
                (sub[time_col] - sub[truth_col]).dt.total_milliseconds().to_numpy() / 1000
            )
            share = float((error <= sub[unc_col].to_numpy() + 1e-9).mean()) * 100
            lines.append(
                f"| {name} | {method} | {sub.height} | {share:.1f} % | {np.median(error):.1f} |"
            )
    return lines


def write_findings(inputs: EventsInputs) -> Path:
    """Write the findings page and its charts; return the page's path."""
    data = inputs.events_dir.parent
    out = data / "findings"
    out.mkdir(parents=True, exist_ok=True)
    files = sorted(inputs.compare_dir.glob("service_date=*/part-0.parquet"))
    page = out / "reported-vs-reconstructed.md"
    synthetic = inputs.ingest.synthetic
    lines = ["# Reported delay against reconstructed delay", ""]
    if synthetic:
        lines += [
            "> **SYNTHETIC DATA.** The archive was generated, so the true delays are known. The "
            "numbers show that the method works; they say nothing about real Prague.",
            "",
        ]
    if not files:
        lines += ["No reconstructed event could be compared with a reported delay yet."]
        page.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return page
    frame = pl.concat([pl.read_parquet(f) for f in files])
    days = sorted({str(d) for d in frame["service_date"].unique().to_list()})
    lines += [
        f"Service dates: {', '.join(days)}. **{frame.height} stop events** were compared.",
        "",
        "The delay of each stop event was reconstructed from the vehicle's positions "
        "(arrival minus scheduled arrival). The source also reports a delay with every "
        "position. Here the report nearest in time (at most "
        f"{inputs.cfg.compare.max_dt_s:.0f} s away) is subtracted: **reported minus "
        "reconstructed**. A positive number means the source says the vehicle is more late than "
        "its positions show. The reconstruction is a second measurement, not the truth, unless the "
        "data is synthetic.",
        "",
    ]
    _by_route_chart(frame, out / "by-route.png")
    _by_position_chart(frame, out / "by-stop-position.png")
    lines += [
        "## By route",
        "",
        "![Reported minus reconstructed delay by route](by-route.png)",
        "",
        *_table(frame, "route_id"),
        "",
        "## By vehicle type",
        "",
        *_table(frame, "route_type", ROUTE_TYPE_NAMES),
        "",
        "## By stop position",
        "",
        "![Reported minus reconstructed delay by stop position](by-stop-position.png)",
        "",
        *_table(frame, "stop_position"),
        "",
    ]
    events_files = sorted(inputs.events_dir.glob("service_date=*/part-0.parquet"))
    truth_path = data.parent / "truth" / "stop_events.parquet"
    if synthetic and events_files and truth_path.exists():
        truth = pl.read_parquet(truth_path).select(
            "trip_id", "stop_sequence", "true_arr", "true_dep"
        )
        events = pl.concat([pl.read_parquet(f) for f in events_files])
        lines += [
            "## How accurate is the reconstruction? (synthetic archive only)",
            "",
            "Only a synthetic archive has the true arrival and departure times. The stated "
            "uncertainty of an event is half the time between the two positions that bracket "
            "it, so it is a bound, not a standard deviation; the typical error is far smaller.",
            "",
            *_accuracy_lines(events, truth),
            "",
        ]
    lines += [
        "## Limits",
        "",
        "- The nearest report can be up to the stated number of seconds from the event, so "
        "a difference of a few seconds is within what this comparison can resolve.",
        "- A vehicle type or a stop position can hide what a single route does: compare the "
        "rows with the table by route before reading anything into a group.",
        "- Events are reconstructed only where positions reach the stop; stops at the start "
        "and end of a trip are often missing one of their two times.",
        "",
    ]
    page.write_text("\n".join(lines), encoding="utf-8")
    return page
