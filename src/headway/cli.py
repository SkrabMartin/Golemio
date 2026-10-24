"""Command-line interface for Headway.

Every pipeline step is a subcommand of ``headway``. Subcommands that belong to a later
phase are registered already, so the CLI surface is stable, but exit with code 2 until
they are implemented.
"""

import asyncio
import signal
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
def ingest() -> None:
    """Parse raw snapshots into the partitioned positions dataset (layer L1)."""
    _not_implemented("F4")


@app.command()
def audit() -> None:
    """Report data-quality findings for collected days (layer L2)."""
    _not_implemented("F5")


@app.command()
def events() -> None:
    """Reconstruct stop arrival and departure events (layer L3)."""
    _not_implemented("F6")


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
