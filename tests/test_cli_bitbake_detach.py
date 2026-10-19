"""Tests for ``--detach`` and lifecycle hints on ``bakar bitbake`` / ``bakar rebuild``."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

import bakar.commands.bitbake  # noqa: F401 - registers the commands on app
from bakar.cli import app

pytestmark = pytest.mark.unit

_MANIFEST = "imx-6.6.52-2.2.0.xml"
_TARGET = "busybox"
_LAUNCHER = "bakar.commands.bitbake._detach_cmd.detach_and_exit"


def _args(ws: Path, *extra: str, cmd: str = "bitbake") -> list[str]:
    return [cmd, _TARGET, *extra, "--manifest", _MANIFEST, "--workspace", str(ws)]


def _live(rc: int):
    return lambda ctx, command: rc


@pytest.mark.parametrize("cmd", ["bitbake", "rebuild"])
def test_detach_hands_launcher_argv_and_cwd(runner, nxp_workspace, cmd) -> None:
    with (
        patch(_LAUNCHER, side_effect=SystemExit(0)) as launcher,
        patch("bakar.commands.bitbake.run_shell_live") as live,
    ):
        result = runner.invoke(app, _args(nxp_workspace, "--detach", cmd=cmd))
    assert result.exit_code == 0, result.output
    launcher.assert_called_once()
    argv, cwd = launcher.call_args.args
    assert isinstance(argv, list)
    assert isinstance(cwd, Path)
    live.assert_not_called()


def test_devshell_with_detach_exits_2_unlaunched(runner, nxp_workspace) -> None:
    with patch(_LAUNCHER) as launcher:
        result = runner.invoke(app, _args(nxp_workspace, "--detach", "--task", "devshell"))
    assert result.exit_code == 2, result.output
    launcher.assert_not_called()


def test_clean_recipe_has_no_detach(runner, nxp_workspace) -> None:
    result = runner.invoke(app, ["clean-recipe", _TARGET, "--detach", "--workspace", str(nxp_workspace)])
    assert result.exit_code == 2


def test_agent_failure_names_run_and_triage(runner, nxp_workspace, monkeypatch) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    with patch("bakar.commands.bitbake.run_shell_live", _live(1)):
        result = runner.invoke(app, _args(nxp_workspace))
    assert result.exit_code == 1
    assert "failed (exit 1, run " in result.output
    assert "bakar triage " in result.output
    assert " --json" in result.output
    assert "run with --detach" in result.output


def test_agent_success_names_report(runner, nxp_workspace, monkeypatch) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    with patch("bakar.commands.bitbake.run_shell_live", _live(0)):
        result = runner.invoke(app, _args(nxp_workspace, cmd="rebuild"))
    assert result.exit_code == 0
    assert "bakar report " in result.output
    assert "bakar triage" not in result.output


def test_unset_env_prints_no_hints(runner, nxp_workspace, monkeypatch) -> None:
    monkeypatch.delenv("CLAUDECODE", raising=False)
    with patch("bakar.commands.bitbake.run_shell_live", _live(1)):
        result = runner.invoke(app, _args(nxp_workspace))
    assert "hint:" not in result.output
    assert "bakar triage" not in result.output


def test_listtasks_gets_no_nudge(runner, nxp_workspace, monkeypatch) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")

    def fake_capture(ctx, command, capture):
        capture.stdout_path.parent.mkdir(parents=True, exist_ok=True)
        capture.stdout_path.write_text("do_compile\n")
        return 0

    with patch("bakar.commands.bitbake.run_shell_capture", fake_capture):
        result = runner.invoke(app, _args(nxp_workspace, "--task", "listtasks"))
    assert result.exit_code == 0, result.output
    assert "run with --detach" not in result.output
