"""Tests for the ``bakar shell`` command.

``step_kas.run_shell`` is patched to capture its arguments and return a chosen
exit code, so no kas or kas-container process starts. The workspace is a tmp
dir with an ``nxp/`` subdirectory and the user config is a clean default, so
the real ``~/.config/bakar/config.toml`` is never read.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

import bakar.commands.shell  # noqa: F401 - registers the command on app
from bakar.cli import app
from bakar.user_config import UserConfig

if TYPE_CHECKING:
    from pathlib import Path

    from typer.testing import CliRunner as _CliRunner

pytestmark = pytest.mark.unit

_MANIFEST = "imx-6.6.52-2.2.0.xml"


@pytest.fixture(autouse=True)
def _clean_user_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("bakar.commands._app._load_user_config_safe", UserConfig)
    monkeypatch.delenv("BAKAR_HOST_MODE", raising=False)
    monkeypatch.delenv("BAKAR_CONTAINER", raising=False)


def _fake_run_shell(rc: int, calls: list[dict]):
    """Return a fake ``run_shell`` recording command, extra args and host_mode."""

    def fake(ctx, args, command=None):
        calls.append({"args": args, "command": command, "host_mode": ctx.cfg.host_mode})
        return rc

    return fake


def _invoke(runner: _CliRunner, workspace: Path, rc: int, *args: str, global_args: tuple[str, ...] = ()):
    calls: list[dict] = []
    with patch("bakar.steps.kas_build.run_shell", _fake_run_shell(rc, calls)):
        result = runner.invoke(
            app,
            [*global_args, "shell", "--manifest", _MANIFEST, "--workspace", str(workspace), *args],
        )
    return result, calls


def test_exit_code_propagates_and_command_is_passed(runner: _CliRunner, nxp_workspace: Path) -> None:
    """``-c CMD`` reaches run_shell as ``command`` and the step's exit code 3 is the process exit code."""
    result, calls = _invoke(runner, nxp_workspace, 3, "-c", "bitbake-layers show-layers")

    assert result.exit_code == 3, result.output
    assert len(calls) == 1
    assert calls[0]["command"] == "bitbake-layers show-layers"
    assert calls[0]["args"] == []


def test_long_command_option(runner: _CliRunner, nxp_workspace: Path) -> None:
    """``--command`` is the long spelling of ``-c``."""
    result, calls = _invoke(runner, nxp_workspace, 0, "--command", "env")

    assert result.exit_code == 0, result.output
    assert calls[0]["command"] == "env"


def test_no_command_means_interactive_shell(runner: _CliRunner, nxp_workspace: Path) -> None:
    """Without ``-c`` the step gets ``command=None`` (interactive shell)."""
    result, calls = _invoke(runner, nxp_workspace, 0)

    assert result.exit_code == 0, result.output
    assert calls[0]["command"] is None
    assert calls[0]["args"] == []


def test_extra_args_are_passed_through(runner: _CliRunner, nxp_workspace: Path) -> None:
    """Args after a positional kas YAML and ``--`` are handed to the step as the passthrough list."""
    kas_yaml = nxp_workspace / "kas.yml"
    kas_yaml.write_text("machine: qemux86-64\n")
    calls: list[dict] = []

    with patch("bakar.steps.kas_build.run_shell", _fake_run_shell(0, calls)):
        result = runner.invoke(
            app,
            ["shell", str(kas_yaml), "-c", "true", "--workspace", str(nxp_workspace), "--", "--flag", "value"],
        )

    assert result.exit_code == 0, result.output
    assert calls[0]["command"] == "true"
    assert calls[0]["args"] == ["--flag", "value"]


def test_global_host_flag_selects_host_mode(runner: _CliRunner, nxp_workspace: Path) -> None:
    """``bakar --host shell`` runs the step with ``host_mode=True``."""
    result, calls = _invoke(runner, nxp_workspace, 0, "-c", "true", global_args=("--host",))

    assert result.exit_code == 0, result.output
    assert calls[0]["host_mode"] is True


def test_global_container_flag_disables_host_mode(runner: _CliRunner, nxp_workspace: Path) -> None:
    """``bakar --container shell`` runs the step with ``host_mode=False``."""
    result, calls = _invoke(runner, nxp_workspace, 0, "-c", "true", global_args=("--container",))

    assert result.exit_code == 0, result.output
    assert calls[0]["host_mode"] is False
