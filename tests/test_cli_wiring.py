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

REQUIRED_COMMANDS = {"show", "getvar", "inspect", "diffsigs", "layers", "drift", "changelog"}


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


def test_malformed_preset_exits_2(runner: CliRunner, monkeypatch) -> None:
    """A malformed [[presets]] entry in config.toml causes typer.Exit(2) at startup.

    --help is an eager flag that skips the callback body; use a subcommand's
    --help to trigger the @app.callback() before Click prints help and exits.
    """
    import bakar.commands._app as _state

    def _bad_load_presets_safe() -> None:
        _state.console.print("[red]Invalid presets config:[/] family 'rockchip' is not valid")
        raise typer.Exit(code=2)

    monkeypatch.setattr(_state, "_load_presets_safe", _bad_load_presets_safe)

    result = runner.invoke(app, ["doctor", "--help"])
    assert result.exit_code == 2


def test_presets_subapp_registered() -> None:
    """The presets sub-app is registered on the shared Typer app."""
    click_app = typer.main.get_command(app)
    assert "presets" in click_app.commands, f"'presets' not in registered groups: {list(click_app.commands.keys())}"
