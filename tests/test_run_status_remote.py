"""``bakar status --on`` and ``bakar wait --on``: remote query over ``bash -s``."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from bakar.cli import app
from bakar.steps import remote_dispatch as rd

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()


def _proc(code: int = 0, out: str = "", err: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=code, stdout=out, stderr=err)


@pytest.fixture
def capture(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    monkeypatch.setattr(rd, "preflight_or_report", lambda host, assume_yes: True)
    mock = MagicMock(return_value=_proc(0, '{"state": "succeeded"}\n'))
    monkeypatch.setattr(rd, "_ssh_bash_capture", mock)
    return mock


def test_status_mirrors_stdout_and_exit(capture: MagicMock) -> None:
    capture.return_value = _proc(2, '{"state": "x"}\n', "remote warning\n")
    result = runner.invoke(app, ["status", "run1", "--json", "--on", "pc2"])
    assert result.exit_code == 2
    assert result.stdout == '{"state": "x"}\n'
    host, script = capture.call_args.args
    assert host == "pc2"
    assert script == "bakar status run1 --json\n"
    assert capture.call_args.kwargs["timeout"] == 30.0


def test_wait_mirrors_exit_and_bounds_capture(capture: MagicMock) -> None:
    capture.return_value = _proc(124, '{"state": "running"}\n')
    result = runner.invoke(app, ["wait", "u1", "--timeout", "540", "--interval", "5", "--json", "--on", "pc2"])
    assert result.exit_code == 124
    assert result.stdout == '{"state": "running"}\n'
    assert capture.call_args.args[1] == "bakar wait u1 --timeout 540.0 --interval 5.0 --json\n"
    assert capture.call_args.kwargs["timeout"] == 600.0


def test_unbounded_wait_has_no_capture_timeout(capture: MagicMock) -> None:
    runner.invoke(app, ["wait", "u1", "--on", "pc2"])
    assert capture.call_args.kwargs["timeout"] is None


def test_transport_failure_exit_codes(capture: MagicMock) -> None:
    capture.return_value = _proc(255, "", "unreachable")
    status = runner.invoke(app, ["status", "run1", "--on", "pc2"])
    wait = runner.invoke(app, ["wait", "run1", "--on", "pc2"])
    assert status.exit_code == 2
    assert wait.exit_code == 3
    assert "255" in status.output
    assert "255" in wait.output


def test_handle_arrives_quoted(capture: MagicMock) -> None:
    handle = "$(touch /tmp/pwned)"
    runner.invoke(app, ["status", handle, "--on", "pc2"])
    script = capture.call_args.args[1]
    assert script == "bakar status '$(touch /tmp/pwned)'\n"


def test_hyphen_host_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    mock = MagicMock()
    monkeypatch.setattr(rd, "_ssh_bash_capture", mock)
    monkeypatch.setattr(rd, "preflight_remote", mock)
    for cmd in ("status", "wait"):
        result = runner.invoke(app, [cmd, "run1", "--on", "-oProxyCommand=x"])
        assert result.exit_code == 2
    mock.assert_not_called()


def test_workspace_with_on_is_refused_before_ssh(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    mock = MagicMock()
    monkeypatch.setattr(rd, "_ssh_bash_capture", mock)
    monkeypatch.setattr(rd, "preflight_remote", mock)
    for cmd in ("status", "wait"):
        result = runner.invoke(app, [cmd, "run1", "--on", "pc2", "-w", str(tmp_path)])
        assert result.exit_code == 2
        assert "--workspace" in result.output
        assert "--on" in result.output
    mock.assert_not_called()
