import pytest
from typer.testing import CliRunner

from headway.cli import NOT_IMPLEMENTED_EXIT_CODE, app

# Wide terminal and no colour so Rich does not wrap or style the help text.
runner = CliRunner(env={"COLUMNS": "200", "NO_COLOR": "1", "TERM": "dumb"})

IMPLEMENTED = ["collect", "snapshot-gtfs"]
PLACEHOLDERS = {
    "ingest": "F4",
    "audit": "F5",
    "events": "F6",
    "metrics": "F7",
    "split": "F8",
    "evaluate": "F9",
    "export-site": "F10",
    "report": "F11",
}


def test_help_lists_all_subcommands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0, result.output
    for command in [*IMPLEMENTED, *PLACEHOLDERS]:
        assert command in result.output


@pytest.mark.parametrize(("command", "phase"), PLACEHOLDERS.items())
def test_placeholder_exits_with_code_2(command: str, phase: str) -> None:
    result = runner.invoke(app, [command])
    assert result.exit_code == NOT_IMPLEMENTED_EXIT_CODE
    assert f"not implemented yet (phase {phase})" in result.output
