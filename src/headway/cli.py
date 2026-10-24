"""Command-line interface for Headway.

Every pipeline step is a subcommand of ``headway``. Subcommands that belong to a later
phase are registered already, so the CLI surface is stable, but exit with code 2 until
they are implemented.
"""

from typing import NoReturn

import typer

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


@app.command()
def collect() -> None:
    """Poll Golemio vehicle positions and store raw snapshots (layer L0)."""
    _not_implemented("F2")


@app.command("snapshot-gtfs")
def snapshot_gtfs() -> None:
    """Archive the current PID static GTFS feed, keyed by content hash."""
    _not_implemented("F3")


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
