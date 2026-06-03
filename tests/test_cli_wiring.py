"""Verify that all new inspection commands are wired into the CLI.

Checks that show, getvar, inspect, diffsigs, and layers are all registered on
the shared Typer app after importing bakar.cli. Uses both a direct Click
command-map lookup and a CliRunner --help invocation to cover both surfaces.
"""

from __future__ import annotations

import pytest
import typer
from typer.testing import CliRunner

from bakar.cli import app

pytestmark = pytest.mark.unit

REQUIRED_COMMANDS = {"show", "getvar", "inspect", "diffsigs", "layers"}


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_all_commands_in_click_map() -> None:
    """All five inspection commands appear in the Click command map."""
    click_app = typer.main.get_command(app)
    registered = set(click_app.commands.keys())
    missing = REQUIRED_COMMANDS - registered
    assert not missing, f"Commands not registered: {missing}"


def test_help_lists_all_commands(runner: CliRunner) -> None:
    """bakar --help output mentions each required command by name."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0, result.output
    for cmd in REQUIRED_COMMANDS:
        assert cmd in result.output, f"'{cmd}' not found in --help output"


def test_import_succeeds() -> None:
    """Importing bakar.cli does not raise."""
    import bakar.cli  # noqa: F401 - import is the test

    assert True
