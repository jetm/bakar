"""Tests for the ``bakar bitbake`` and ``bakar clean-recipe`` commands.

Drives both commands through the Typer ``CliRunner``. Container exec is
monkeypatched so no real kas-container runs:

- The generic (non-devshell/non-listtasks) path is patched via
  ``patch("bakar.commands.bitbake.run_shell_live")`` - it streams live output
  and returns an exit code rather than capturing to a log.
- ``--task listtasks`` is patched via
  ``patch("bakar.commands.bitbake.run_shell_capture")`` - it still parses the
  captured output.
- ``--task devshell`` is patched via ``patch("bakar.commands.bitbake.run_shell")``.

These let the tests verify:

- A plain target issues ``bitbake busybox`` via the live path.
- ``-c compile`` issues ``bitbake -c compile busybox``.
- ``-k`` appends ``-k`` to the command.
- ``clean-recipe busybox`` issues ``bitbake -c cleansstate busybox``.
- A non-zero live exit propagates.
- ``--task devshell`` routes through the interactive ``run_shell`` helper and
  not the live helper.
- ``--task listtasks`` parses captured output into task names.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

import bakar.commands.bitbake  # noqa: F401 - registers the commands on app
from bakar.cli import app

if TYPE_CHECKING:
    from pathlib import Path

    from typer.testing import CliRunner as _CliRunner

pytestmark = pytest.mark.unit

_MANIFEST = "imx-6.6.52-2.2.0.xml"
_TARGET = "busybox"


@pytest.fixture
def runner() -> _CliRunner:
    from typer.testing import CliRunner

    return CliRunner()


@pytest.fixture
def nxp_workspace(tmp_path: Path) -> Path:
    """Minimal NXP workspace so ``_resolve_workspace`` succeeds."""
    (tmp_path / "nxp").mkdir()
    return tmp_path


def _make_fake_capture(text: str, rc: int, calls: list[dict]):
    """Return a fake ``run_shell_capture`` writing ``text`` and returning ``rc``."""

    def fake_capture(ctx, command, stdout_path, *, step="kas_shell_capture", python_executable=None):
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stdout_path.write_text(text)
        calls.append({"command": command, "stdout_path": stdout_path})
        return rc

    return fake_capture


def _make_fake_live(rc: int, calls: list[dict]):
    """Return a fake ``run_shell_live`` recording its ``command`` and returning ``rc``."""

    def fake_live(ctx, command):
        calls.append({"command": command})
        return rc

    return fake_live


def _make_fake_shell(rc: int, calls: list[dict]):
    """Return a fake ``run_shell`` recording its ``command`` and returning ``rc``."""

    def fake_shell(ctx, args, command=None):
        calls.append({"command": command, "args": args})
        return rc

    return fake_shell


# ---------------------------------------------------------------------------
# Command construction
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_plain_target_builds_bitbake_busybox(runner: _CliRunner, nxp_workspace: Path) -> None:
    """A plain target issues ``bitbake busybox`` via the live path."""
    live_calls: list[dict] = []
    fake = _make_fake_live(0, live_calls)

    with patch("bakar.commands.bitbake.run_shell_live", fake):
        result = runner.invoke(
            app,
            ["bitbake", _TARGET, "--manifest", _MANIFEST, "--workspace", str(nxp_workspace)],
        )

    assert result.exit_code == 0, result.output
    assert len(live_calls) == 1
    assert live_calls[0]["command"] == "bitbake busybox"


@pytest.mark.unit
def test_task_compile_builds_dash_c_compile(runner: _CliRunner, nxp_workspace: Path) -> None:
    """``-c compile`` issues ``bitbake -c compile busybox``."""
    live_calls: list[dict] = []
    fake = _make_fake_live(0, live_calls)

    with patch("bakar.commands.bitbake.run_shell_live", fake):
        result = runner.invoke(
            app,
            ["bitbake", _TARGET, "-c", "compile", "--manifest", _MANIFEST, "--workspace", str(nxp_workspace)],
        )

    assert result.exit_code == 0, result.output
    assert live_calls[0]["command"] == "bitbake -c compile busybox"


@pytest.mark.unit
def test_keep_going_appends_dash_k(runner: _CliRunner, nxp_workspace: Path) -> None:
    """``-k`` appends ``-k`` to the issued command."""
    live_calls: list[dict] = []
    fake = _make_fake_live(0, live_calls)

    with patch("bakar.commands.bitbake.run_shell_live", fake):
        result = runner.invoke(
            app,
            ["bitbake", _TARGET, "-k", "--manifest", _MANIFEST, "--workspace", str(nxp_workspace)],
        )

    assert result.exit_code == 0, result.output
    assert "-k" in live_calls[0]["command"].split()
    assert live_calls[0]["command"] == "bitbake -k busybox"


# ---------------------------------------------------------------------------
# clean-recipe
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_clean_recipe_issues_cleansstate(runner: _CliRunner, nxp_workspace: Path) -> None:
    """``clean-recipe busybox`` issues ``bitbake -c cleansstate busybox``."""
    live_calls: list[dict] = []
    fake = _make_fake_live(0, live_calls)

    with patch("bakar.commands.bitbake.run_shell_live", fake):
        result = runner.invoke(
            app,
            ["clean-recipe", _TARGET, "--manifest", _MANIFEST, "--workspace", str(nxp_workspace)],
        )

    assert result.exit_code == 0, result.output
    assert live_calls[0]["command"] == "bitbake -c cleansstate busybox"


# ---------------------------------------------------------------------------
# Exit code propagation
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_nonzero_live_exit_propagates(runner: _CliRunner, nxp_workspace: Path) -> None:
    """A non-zero live exit propagates as the command exit code."""
    live_calls: list[dict] = []
    fake = _make_fake_live(1, live_calls)

    with patch("bakar.commands.bitbake.run_shell_live", fake):
        result = runner.invoke(
            app,
            ["bitbake", _TARGET, "--manifest", _MANIFEST, "--workspace", str(nxp_workspace)],
        )

    assert result.exit_code != 0, result.output


# ---------------------------------------------------------------------------
# devshell routing
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_devshell_routes_through_interactive_helper(runner: _CliRunner, nxp_workspace: Path) -> None:
    """``--task devshell`` uses the interactive ``run_shell``, not the capture helper."""
    capture_calls: list[dict] = []
    shell_calls: list[dict] = []
    fake_capture = _make_fake_capture("", 0, capture_calls)
    fake_shell = _make_fake_shell(0, shell_calls)

    with (
        patch("bakar.commands.bitbake.run_shell_capture", fake_capture),
        patch("bakar.commands.bitbake.run_shell", fake_shell),
    ):
        result = runner.invoke(
            app,
            ["bitbake", _TARGET, "-c", "devshell", "--manifest", _MANIFEST, "--workspace", str(nxp_workspace)],
        )

    assert result.exit_code == 0, result.output
    assert len(shell_calls) == 1
    assert len(capture_calls) == 0
    assert shell_calls[0]["command"] == "bitbake -c devshell busybox"


# ---------------------------------------------------------------------------
# listtasks pretty-print
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_listtasks_pretty_prints_task_names(runner: _CliRunner, nxp_workspace: Path) -> None:
    """``--task listtasks`` parses and prints the recipe's task names."""
    capture_calls: list[dict] = []
    listtasks_out = "do_compile\ndo_install\ndo_fetch\n"
    fake = _make_fake_capture(listtasks_out, 0, capture_calls)

    with patch("bakar.commands.bitbake.run_shell_capture", fake):
        result = runner.invoke(
            app,
            ["bitbake", _TARGET, "-c", "listtasks", "--manifest", _MANIFEST, "--workspace", str(nxp_workspace)],
        )

    assert result.exit_code == 0, result.output
    assert capture_calls[0]["command"] == "bitbake -c listtasks busybox"
    assert "do_compile" in result.output
    assert "do_install" in result.output
