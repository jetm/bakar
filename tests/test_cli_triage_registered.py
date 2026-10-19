"""``bakar triage`` for a run registered by a detached unit, and ``triage --on``."""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from bakar import detach_records
from bakar.cli import app
from bakar.commands import status as status_mod
from bakar.steps import remote_dispatch as rd
from tests.conftest import SAMPLE_EVENTS_JSONL, SAMPLE_KAS_LOG

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit

runner = CliRunner()

UNIT = "bakar-detach-20261019-120000-abc123"
RUN_ID = "20261019-120001-4242"


@pytest.fixture(autouse=True)
def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Runtime dir for registrations, an empty HOME, and a cwd outside any workspace."""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)
    return tmp_path


def _registered_run(root: Path, run_id: str = RUN_ID) -> Path:
    run = root / "elsewhere" / "build" / "runs" / run_id
    run.mkdir(parents=True)
    (run / "events.jsonl").write_text(SAMPLE_EVENTS_JSONL)
    (run / "kas.log").write_text(SAMPLE_KAS_LOG)
    assert detach_records.append_registration(UNIT, run_id, run, 1)
    return run


def test_registered_run_resolves_outside_any_workspace(_env: Path) -> None:
    _registered_run(_env)
    result = runner.invoke(app, ["triage", RUN_ID, "--json"])
    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout)
    assert doc["run_id"] == RUN_ID
    assert doc["failing_step"] == "kas-build"


def test_unregistered_unfound_id_keeps_the_workspace_error(_env: Path) -> None:
    result = runner.invoke(app, ["triage", "20261019-999999-1", "--json"])
    assert result.exit_code == 2
    assert "Not inside a BSP workspace" in result.output


def test_registered_run_is_the_fallback_inside_a_workspace(_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _registered_run(_env)
    ws = _env / "ws"
    (ws / "nxp" / "build" / "runs").mkdir(parents=True)
    (ws / ".bakar.toml").write_text("")
    monkeypatch.chdir(ws)
    result = runner.invoke(app, ["triage", RUN_ID, "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["run_id"] == RUN_ID


def test_workspace_lookup_wins_over_the_registry(_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _registered_run(_env)
    ws = _env / "ws"
    local = ws / "nxp" / "build" / "runs" / RUN_ID
    local.mkdir(parents=True)
    (ws / ".bakar.toml").write_text("")
    (local / "events.jsonl").write_text(
        '{"event": "step_fail", "step": "local-step", "reason": "boom", "ts": "2026-05-29T12:00:00Z"}\n'
    )
    (local / "kas.log").write_text("")
    monkeypatch.chdir(ws)
    result = runner.invoke(app, ["triage", RUN_ID, "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["failing_step"] == "local-step"


def test_no_run_id_outside_a_workspace_is_unchanged(_env: Path) -> None:
    _registered_run(_env)
    result = runner.invoke(app, ["triage"])
    assert result.exit_code == 2
    assert "Not inside a BSP workspace" in result.output


def _proc(code: int = 0, out: str = "", err: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=code, stdout=out, stderr=err)


@pytest.fixture
def capture(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    monkeypatch.setattr(status_mod, "_preflight", lambda host, *, transport_exit: None)
    mock = MagicMock(return_value=_proc(0, '{"run_id": "r"}\n'))
    monkeypatch.setattr(rd, "_ssh_bash_capture", mock)
    return mock


def test_on_mirrors_stdout_and_exit_status(capture: MagicMock) -> None:
    capture.return_value = _proc(1, '{"run_id": "r", "host": null}\n', "remote warning\n")
    result = runner.invoke(app, ["triage", "r", "--json", "--on", "pc2"])
    assert result.exit_code == 1
    # Triage JSON has no host field to fill; the remote stdout is mirrored as is.
    assert result.stdout == '{"run_id": "r", "host": null}\n'
    host, script = capture.call_args.args
    assert host == "pc2"
    assert script == "bakar triage --json -- r\n"


def test_on_without_run_id_or_json(capture: MagicMock) -> None:
    runner.invoke(app, ["triage", "--on", "pc2"])
    assert capture.call_args.args[1] == "bakar triage\n"


def test_on_quotes_the_run_id(capture: MagicMock) -> None:
    runner.invoke(app, ["triage", "$(touch /tmp/pwned)", "--on", "pc2"])
    assert capture.call_args.args[1] == "bakar triage -- '$(touch /tmp/pwned)'\n"


def test_on_forwards_preset_and_release(capture: MagicMock) -> None:
    runner.invoke(app, ["triage", "--preset", "imx93", "--release", "scarthgap", "--json", "--on", "pc2"])
    assert capture.call_args.args[1] == "bakar triage --json --preset imx93 --release scarthgap\n"


def test_on_refuses_kas_yaml_before_ssh(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    mock = MagicMock()
    monkeypatch.setattr(rd, "_ssh_bash_capture", mock)
    monkeypatch.setattr(rd, "preflight_remote", mock)
    result = runner.invoke(app, ["triage", "--kas-yaml", str(tmp_path / "x.yml"), "--on", "pc2"])
    assert result.exit_code == 2
    assert "--kas-yaml" in result.output
    assert "--on" in result.output
    mock.assert_not_called()


def test_dash_run_id_cannot_become_a_remote_flag(capture: MagicMock) -> None:
    runner.invoke(app, ["triage", "--on", "pc2", "--", "--json"])
    assert capture.call_args.args[1] == "bakar triage -- --json\n"


def test_unit_handle_resolves_to_its_latest_run(_env: Path) -> None:
    older = "20261019-110000-1"
    _registered_run(_env, older)
    _registered_run(_env, RUN_ID)
    result = runner.invoke(app, ["triage", UNIT, "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["run_id"] == RUN_ID
    assert RUN_ID in result.stderr
    assert UNIT in result.stderr


def test_unit_handle_without_a_registered_run_is_not_found(_env: Path) -> None:
    result = runner.invoke(app, ["triage", UNIT])
    assert result.exit_code == 1
    assert "no registered run" in result.output


def test_on_refuses_a_dash_host_before_ssh(monkeypatch: pytest.MonkeyPatch) -> None:
    mock = MagicMock()
    monkeypatch.setattr(rd, "_ssh_bash_capture", mock)
    monkeypatch.setattr(rd, "preflight_remote", mock)
    result = runner.invoke(app, ["triage", "r", "--on", "-oProxyCommand=x"])
    assert result.exit_code == 2
    mock.assert_not_called()


def test_on_with_workspace_is_refused_before_ssh(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    mock = MagicMock()
    monkeypatch.setattr(rd, "_ssh_bash_capture", mock)
    monkeypatch.setattr(rd, "preflight_remote", mock)
    result = runner.invoke(app, ["triage", "r", "--on", "pc2", "-w", str(tmp_path)])
    assert result.exit_code == 2
    assert "--workspace" in result.output
    assert "--on" in result.output
    mock.assert_not_called()


def test_on_unreachable_host_exits_2_naming_255(monkeypatch: pytest.MonkeyPatch) -> None:
    mock = MagicMock(return_value=_proc(255, "", "ssh: Could not resolve hostname"))
    monkeypatch.setattr(rd, "_ssh_bash_capture", mock)
    monkeypatch.setattr(rd, "check_versions", lambda version, *, assume_yes: True)
    result = runner.invoke(app, ["triage", "r", "--on", "no-such-host.invalid"])
    assert result.exit_code == 2
    assert "ssh exit status 255" in result.output
    assert mock.call_count == 1
    assert "--version" in mock.call_args.args[1]
