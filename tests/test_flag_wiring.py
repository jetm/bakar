"""Tests for the global --plain/--ci/--rich output-mode override flags."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

import bakar.cli  # noqa: F401 - registers all subcommands on the shared app
import bakar.commands._app as _state
from bakar.commands._app import app
from bakar.commands._helpers import global_output_mode_override
from bakar.output_mode import OutputMode

runner = CliRunner()


@pytest.fixture(autouse=True)
def _restore_output_mode_override() -> None:
    """Save and restore the module global so tests can't leak override state."""
    saved = _state._OUTPUT_MODE_OVERRIDE
    try:
        yield
    finally:
        _state._OUTPUT_MODE_OVERRIDE = saved


def test_plain_flag_sets_override() -> None:
    _state._OUTPUT_MODE_OVERRIDE = None
    result = runner.invoke(app, ["--plain", "doctor", "--help"])
    assert result.exit_code == 0
    assert _state._OUTPUT_MODE_OVERRIDE is OutputMode.PLAIN
    assert global_output_mode_override() is OutputMode.PLAIN


def test_ci_alias_sets_plain_override() -> None:
    _state._OUTPUT_MODE_OVERRIDE = None
    result = runner.invoke(app, ["--ci", "doctor", "--help"])
    assert result.exit_code == 0
    assert _state._OUTPUT_MODE_OVERRIDE is OutputMode.PLAIN


def test_rich_flag_sets_override() -> None:
    _state._OUTPUT_MODE_OVERRIDE = None
    result = runner.invoke(app, ["--rich", "doctor", "--help"])
    assert result.exit_code == 0
    assert _state._OUTPUT_MODE_OVERRIDE is OutputMode.RICH


def test_no_flag_leaves_override_none() -> None:
    _state._OUTPUT_MODE_OVERRIDE = OutputMode.PLAIN
    result = runner.invoke(app, ["doctor", "--help"])
    assert result.exit_code == 0
    assert _state._OUTPUT_MODE_OVERRIDE is None


def test_both_flags_exit_2() -> None:
    result = runner.invoke(app, ["--plain", "--rich", "doctor", "--help"])
    assert result.exit_code == 2
