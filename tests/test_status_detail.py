"""Tests for ``bakar status --detail``: the output contract, single progress read and ``--on``."""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

import bakar.commands.status as status_cmd
from bakar.commands._app import app
from bakar.steps import remote_dispatch as rd

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit

RUN_ID = "20261019-120001-4242"
DETAIL_KEYS = {"running_tasks", "failed_tasks", "last_line", "disk_free_gb", "mem_available_mb", "pressure"}


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    runs = tmp_path / "ws" / "build" / "runs"
    runs.mkdir(parents=True)
    monkeypatch.setattr(
        status_cmd, "_resolve_triage_dirs", lambda _kas, _ws: ([(runs, "generic")], tmp_path, "label", tmp_path)
    )
    monkeypatch.setattr(status_cmd, "_unit_active", lambda _u: False)
    monkeypatch.setattr(status_cmd, "is_build_running", lambda _rd: (False, None, False))
    monkeypatch.setattr(status_cmd, "_frontend_alive", lambda _rd: False)
    return tmp_path


def _run(env: Path) -> Path:
    run = env / "ws" / "build" / "runs" / RUN_ID
    run.mkdir(parents=True)
    (run / "events.jsonl").write_text(json.dumps({"event": "run_end", "exit_code": 0}) + "\n")
    (run / "kas.log").write_text("first\nNOTE: Running task 5 of 9\n")
    return run


def test_without_detail_record_has_exactly_twelve_keys(env: Path) -> None:
    run = _run(env)
    result = CliRunner().invoke(app, ["status", run.name, "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert len(payload) == 12
    assert "detail" not in payload


def test_detail_json_has_thirteen_keys_and_six_detail_keys(env: Path) -> None:
    run = _run(env)
    result = CliRunner().invoke(app, ["status", run.name, "--detail", "--json"])
    assert result.exit_code == 0
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert len(payload) == 13
    assert set(payload["detail"]) == DETAIL_KEYS
    assert payload["detail"]["last_line"] == "NOTE: Running task 5 of 9"


def test_unresolved_handle_with_detail_exits_two_with_empty_stdout(env: Path) -> None:
    result = CliRunner().invoke(app, ["status", "nope", "--detail", "--json"])
    assert result.exit_code == 2
    assert result.stdout == ""


def test_human_detail_writes_nothing_to_stdout(env: Path) -> None:
    run = _run(env)
    result = CliRunner().invoke(app, ["status", run.name, "--detail"])
    assert result.exit_code == 0
    assert result.stdout == ""
    assert 3 <= len(result.stderr.splitlines()) <= 5


def test_build_progress_runs_once_per_detail_json(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run = _run(env)
    real = status_cmd._build_progress
    calls: list[Path] = []

    def counting(run_dir: Path) -> dict[str, Any]:
        calls.append(run_dir)
        return real(run_dir)

    monkeypatch.setattr(status_cmd, "_build_progress", counting)
    result = CliRunner().invoke(app, ["status", run.name, "--detail", "--json"])
    assert result.exit_code == 0
    assert len(calls) == 1


def _proc(code: int = 0, out: str = "", err: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=code, stdout=out, stderr=err)


def test_on_host_sends_detail_and_fills_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(status_cmd, "_preflight", lambda host, *, transport_exit: None)
    mock = MagicMock(return_value=_proc(0, '{"state": "running", "host": null}\n'))
    monkeypatch.setattr(rd, "_ssh_bash_capture", mock)
    result = CliRunner().invoke(app, ["status", "run1", "--json", "--detail", "--on", "pc2"])
    assert result.exit_code == 0
    assert mock.call_args.args == ("pc2", "bakar status --json --detail -- run1\n")
    assert json.loads(result.stdout)["host"] == "pc2"


def _version_env(monkeypatch: pytest.MonkeyPatch, remote: str, local: str) -> MagicMock:
    mock = MagicMock(side_effect=[_proc(0, remote + "\n"), _proc(0, '{"state": "running", "host": null}\n')])
    monkeypatch.setattr(rd, "_ssh_bash_capture", mock)
    monkeypatch.setattr(rd, "_local_bakar_version", lambda: local)
    return mock


def test_different_version_remote_is_refused_without_receiving_the_command(monkeypatch: pytest.MonkeyPatch) -> None:
    mock = _version_env(monkeypatch, "bakar 0.40.0 (aaaaaaaaaaaa)", "bakar 0.39.0 (bbbbbbbbbbbb)")
    result = CliRunner().invoke(app, ["status", "run1", "--json", "--detail", "--on", "pc2"])
    assert result.exit_code == 2
    assert mock.call_count == 1
    assert "bakar status" not in mock.call_args.args[1]


def test_same_version_remote_warns_and_receives_the_command(monkeypatch: pytest.MonkeyPatch) -> None:
    mock = _version_env(monkeypatch, "bakar 0.39.0 (aaaaaaaaaaaa)", "bakar 0.39.0 (bbbbbbbbbbbb)")
    result = CliRunner().invoke(app, ["status", "run1", "--json", "--detail", "--on", "pc2"])
    assert result.exit_code == 0
    assert "same version, different build" in result.stderr
    assert mock.call_count == 2
    assert mock.call_args.args[1] == "bakar status --json --detail -- run1\n"


def test_running_build_with_error_last_line_stays_running(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run = env / "ws" / "build" / "runs" / RUN_ID
    run.mkdir(parents=True)
    (run / "events.jsonl").write_text(json.dumps({"event": "step_start", "step": "kas_build"}) + "\n")
    (run / "kas.log").write_text("NOTE: x\nERROR: Task do_compile failed\n")
    monkeypatch.setattr(status_cmd, "_frontend_alive", lambda _rd: True)
    result = CliRunner().invoke(app, ["status", run.name, "--detail", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["state"] == "running"
    assert payload["exit_code"] is None
    assert payload["failure"] is None
    assert payload["detail"]["last_line"] == "ERROR: Task do_compile failed"


def test_unresolved_handle_with_detail_names_the_handle_on_stderr(env: Path) -> None:
    result = CliRunner().invoke(app, ["status", "nope", "--detail"])
    assert result.exit_code == 2
    assert result.stdout == ""
    assert "nope" in result.stderr
