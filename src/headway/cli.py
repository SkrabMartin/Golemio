"""Command-line interface for Headway.

Every pipeline step is a subcommand of ``headway``. Subcommands that belong to a later
phase are registered already, so the CLI surface is stable, but exit with code 2 until
they are implemented.
"""

import asyncio
import signal
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from headway import jsonlog
from headway.config import DEFAULT_CONFIG_PATH, TOKEN_ENV_VAR, golemio_token, load_settings

app = typer.Typer(
    name="headway",
    help="Reliability lab for Prague public transport (PID) built on Golemio data.",
    no_args_is_help=True,
    add_completion=False,
)

NOT_IMPLEMENTED_EXIT_CODE = 2


def _not_implemented(phase: str) -> NoReturn:
    """Report that a subcommand is a placeholder and exit with code 2."""
    typer.echo(f"not implemented yet (phase {phase})", err=True)
    raise typer.Exit(code=NOT_IMPLEMENTED_EXIT_CODE)


ConfigOption = Annotated[
    Path, typer.Option("--config", help="TOML configuration file.", show_default=True)
]


def _install_stop_handlers(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):  # e.g. Windows event loops
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))


@app.command()
def collect(
    once: Annotated[bool, typer.Option("--once", help="Poll a single time and exit.")] = False,
    config: ConfigOption = DEFAULT_CONFIG_PATH,
) -> None:
    """Poll Golemio vehicle positions and store raw snapshots (layer L0)."""
    from headway.collect import Collector, ManifestWriter

    jsonlog.configure()
    settings = load_settings(config)
    token = golemio_token()
    if token is None:
        typer.echo(f"{TOKEN_ENV_VAR} is not set", err=True)
        raise typer.Exit(code=1)
    collector = Collector(
        cfg=settings.collect,
        raw_dir=settings.paths.raw_dir,
        manifest=ManifestWriter(settings.paths.manifest_dir),
        token=token,
    )
    if once:
        record = asyncio.run(collector.once())
        raise typer.Exit(code=0 if record.ok else 1)

    async def _run() -> None:
        stop = asyncio.Event()
        _install_stop_handlers(stop)
        await collector.run(stop)

    asyncio.run(_run())


@app.command("snapshot-gtfs")
def snapshot_gtfs(
    rebuild_registry: Annotated[
        bool,
        typer.Option("--rebuild-registry", help="Re-hash all stored zips instead of downloading."),
    ] = False,
    config: ConfigOption = DEFAULT_CONFIG_PATH,
) -> None:
    """Archive the current PID static GTFS feed, keyed by content hash."""
    from headway import gtfs

    jsonlog.configure()
    settings = load_settings(config)
    gtfs_dir = settings.paths.gtfs_dir
    if rebuild_registry:
        versions = gtfs.rebuild_registry(gtfs_dir)
        typer.echo(f"registry rebuilt: {len(versions)} snapshot(s)")
        return
    result = gtfs.snapshot_gtfs(settings.gtfs.url, gtfs_dir, timeout_s=settings.gtfs.timeout_s)
    if result.stored and result.version is not None:
        v = result.version
        typer.echo(
            f"stored {v.path} ({result.bytes} bytes), "
            f"covers {v.feed_start_date} to {v.feed_end_date}"
        )
    else:
        typer.echo(f"unchanged ({result.sha256[:12]}, {result.bytes} bytes); nothing stored")


@app.command()
def synth(
    out: Annotated[
        Path | None, typer.Option("--out", help="Output directory (default from config).")
    ] = None,
    days: Annotated[int | None, typer.Option("--days", help="Override the number of days.")] = None,
    hours: Annotated[
        float | None, typer.Option("--hours", help="Only generate this many hours of polls.")
    ] = None,
    seed: Annotated[int | None, typer.Option("--seed", help="Override the random seed.")] = None,
    start_at: Annotated[
        datetime | None,
        typer.Option("--start-at", help="Start of the polled window, UTC (with --hours)."),
    ] = None,
    synth_config: Annotated[
        Path, typer.Option("--synth-config", help="Generator parameters (TOML).")
    ] = Path("config/synth.toml"),
) -> None:
    """Generate a SYNTHETIC raw archive with known ground truth (for demos and testing)."""
    import time

    from headway.synth import generate, load_synth_config

    cfg = load_synth_config(synth_config)
    updates: dict[str, object] = {}
    if days is not None:
        updates["days"] = days
    if seed is not None:
        updates["seed"] = seed
    if updates:
        cfg = cfg.model_validate({**cfg.model_dump(), **updates})
    target = out or cfg.output_dir
    started = time.perf_counter()
    try:
        when = start_at.replace(tzinfo=UTC) if start_at and start_at.tzinfo is None else start_at
        result = generate(cfg, target, start_at=when, hours=hours)
    except FileExistsError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    seconds = time.perf_counter() - started
    typer.echo(
        f"SYNTHETIC archive in {target}: {result.polls} polls, {result.files} files, "
        f"{result.trips} trips, {result.defects} planted defects, "
        f"{result.stored_bytes / 1e6:.1f} MB stored ({result.raw_bytes / 1e6:.1f} MB raw), "
        f"{seconds:.1f} s"
    )


@app.command()
def ingest(
    service_date: Annotated[
        str | None, typer.Option("--date", help="Service date to ingest, YYYY-MM-DD.")
    ] = None,
    all_dates: Annotated[
        bool, typer.Option("--all", help="Ingest every service date that has raw polls.")
    ] = False,
    archive: Annotated[
        Path,
        typer.Option(
            "--archive",
            help="Archive root holding raw/ and gtfs/; positions go to <archive>/data/.",
        ),
    ] = Path(),
) -> None:
    """Parse raw snapshots into the partitioned positions dataset (layer L1)."""
    from datetime import date

    from headway.ingest.pipeline import available_service_dates, ingest_dates, inputs_for

    if (service_date is None) == (not all_dates):
        raise typer.BadParameter("give exactly one of --date or --all")
    inputs = inputs_for(archive, load_settings())
    if service_date is not None:
        try:
            days = [date.fromisoformat(service_date)]
        except ValueError as exc:
            raise typer.BadParameter(f"not a date (YYYY-MM-DD): {service_date}") from exc
    else:
        days = available_service_dates(inputs)
        if not days:
            typer.echo(f"no manifest records under {inputs.manifest_dir}", err=True)
            raise typer.Exit(code=1)
    label = "SYNTHETIC " if inputs.synthetic else ""
    for report in ingest_dates(days, inputs):
        bench = report["benchmark"]
        typer.echo(
            f"{label}{report['service_date']}: {report['positions']} positions, "
            f"{report['repeated_observations_dropped']} repeats dropped, "
            f"{report['handover_duplicate_rows']} handover rows, {report['rejects']} rejects "
            f"{report['reject_reasons'] or ''}, {report['owned_polls_at_limit']} polls at limit, "
            f"{bench['seconds']} s"
        )


@app.command()
def audit(
    service_date: Annotated[
        str | None, typer.Option("--date", help="Service date to audit, YYYY-MM-DD.")
    ] = None,
    all_dates: Annotated[
        bool, typer.Option("--all", help="Audit every service date that has raw polls.")
    ] = False,
    archive: Annotated[
        Path,
        typer.Option("--archive", help="Archive root; results go to <archive>/data/audit/."),
    ] = Path(),
    audit_config: Annotated[
        Path, typer.Option("--audit-config", help="Audit thresholds (TOML).")
    ] = Path("config/audit.toml"),
) -> None:
    """Report data-quality findings for collected days (layer L2)."""
    from datetime import date

    from headway.audit.config import load_audit_config
    from headway.audit.pipeline import audit_dates, audit_inputs_for
    from headway.ingest.pipeline import available_service_dates

    if (service_date is None) == (not all_dates):
        raise typer.BadParameter("give exactly one of --date or --all")
    inputs = audit_inputs_for(archive, load_settings(), load_audit_config(audit_config))
    if service_date is not None:
        try:
            days = [date.fromisoformat(service_date)]
        except ValueError as exc:
            raise typer.BadParameter(f"not a date (YYYY-MM-DD): {service_date}") from exc
    else:
        days = available_service_dates(inputs.ingest)
        if not days:
            typer.echo(f"no manifest records under {inputs.ingest.manifest_dir}", err=True)
            raise typer.Exit(code=1)
    label = "SYNTHETIC " if inputs.ingest.synthetic else ""
    for summary in audit_dates(days, inputs):
        per_category = ", ".join(
            f"{name} {info['count']}" for name, info in summary["findings"].items() if info["count"]
        )
        typer.echo(
            f"{label}{summary['service_date']}: {summary['findings_total']} findings"
            f"{' (' + per_category + ')' if per_category else ''}; "
            f"report in {inputs.audit_dir / ('service_date=' + summary['service_date'])}"
        )


@app.command()
def events(
    service_date: Annotated[
        str | None, typer.Option("--date", help="Service date to reconstruct, YYYY-MM-DD.")
    ] = None,
    all_dates: Annotated[
        bool, typer.Option("--all", help="Reconstruct every service date that has raw polls.")
    ] = False,
    archive: Annotated[
        Path,
        typer.Option("--archive", help="Archive root; results go to <archive>/data/events/."),
    ] = Path(),
    events_config: Annotated[
        Path, typer.Option("--events-config", help="Reconstruction parameters (TOML).")
    ] = Path("config/events.toml"),
    findings: Annotated[
        bool,
        typer.Option(
            "--findings", help="Also write the reported-vs-reconstructed page and its charts."
        ),
    ] = False,
) -> None:
    """Reconstruct arrival and departure times at stops from positions (layer L3)."""
    from datetime import date

    from headway.events.config import load_events_config
    from headway.events.pipeline import events_inputs_for, reconstruct_dates
    from headway.ingest.pipeline import available_service_dates

    if (service_date is None) == (not all_dates):
        raise typer.BadParameter("give exactly one of --date or --all")
    inputs = events_inputs_for(archive, load_settings(), load_events_config(events_config))
    if service_date is not None:
        try:
            days = [date.fromisoformat(service_date)]
        except ValueError as exc:
            raise typer.BadParameter(f"not a date (YYYY-MM-DD): {service_date}") from exc
    else:
        days = available_service_dates(inputs.ingest)
        if not days:
            typer.echo(f"no manifest records under {inputs.ingest.manifest_dir}", err=True)
            raise typer.Exit(code=1)
    label = "SYNTHETIC " if inputs.ingest.synthetic else ""
    for report in reconstruct_dates(days, inputs):
        excluded = report["exclusion_reasons"] or ""
        typer.echo(
            f"{label}{report['service_date']}: {report['events']} events from "
            f"{report['trips_reconstructed']} of {report['trips_with_positions']} trips "
            f"({report['trips_excluded']} excluded {excluded}), "
            f"{report['comparison']['n']} compared with the reported delay, "
            f"{report['seconds']} s"
        )
    if findings:
        from headway.events.findings import write_findings

        typer.echo(f"findings page: {write_findings(inputs)}")


@app.command()
def metrics() -> None:
    """Compute reliability and delay-propagation metrics (layer L4)."""
    _not_implemented("F7")


@app.command()
def split() -> None:
    """Build the chronological train, validation and holdout split (layer L5)."""
    _not_implemented("F8")


@app.command()
def evaluate() -> None:
    """Evaluate baselines and nowcast models (layer L5)."""
    _not_implemented("F9")


@app.command("export-site")
def export_site() -> None:
    """Export precomputed aggregates for the static website (layer L6)."""
    _not_implemented("F10")


@app.command()
def report() -> None:
    """Generate the project report from recorded results."""
    _not_implemented("F11")
