"""Triage reports the same failure reason as ``bakar status``."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from bakar import observability
from bakar.cli import app
from bakar.triage import analyse

if TYPE_CHECKING:
    from pathlib import Path

    from typer.testing import CliRunner as _CliRunner

pytestmark = pytest.mark.unit

ERROR_LINE = "ERROR: Nothing PROVIDES 'does-not-exist'"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("home")))


def _events(*recs: dict) -> str:
    return "".join(json.dumps(r) + "\n" for r in recs)


def _step_fail(**extra: object) -> dict:
    return {"event": "step_fail", "step": "kas_build", "exit_code": 2, **extra}


def _make_run(
    tmp_path: Path, *recs: dict, error_report: bool = False, stopped: bool = False, name: str = "20260529-120000"
) -> Path:
    (tmp_path / ".bakar.toml").write_text("")
    run = tmp_path / "nxp" / "build" / "runs" / name
    run.mkdir(parents=True)
    (run / "events.jsonl").write_text(_events(*recs))
    (run / "kas.log").write_text("NOTE: nothing\n")
    if error_report:
        (run / "error-report.json").write_text(
            json.dumps({"step": "kas_build", "kas_log_tail": [], "recipe_errors": [], "suggestions": []})
        )
    if stopped:
        assert observability.write_stop_request(run)
    return run


@pytest.mark.parametrize("error_report", [False, True])
def test_error_field_is_the_reason(tmp_path: Path, error_report: bool) -> None:
    run = _make_run(tmp_path, _step_fail(reason="exit_code=2", error=ERROR_LINE), error_report=error_report)
    report = analyse(run, tmp_path)
    assert report.failing_step == "kas_build"
    assert report.fail_reason == ERROR_LINE


@pytest.mark.parametrize("error_report", [False, True])
def test_reason_only_event_gives_the_reason(tmp_path: Path, error_report: bool) -> None:
    run = _make_run(tmp_path, _step_fail(reason="exit_code=2"), error_report=error_report)
    assert analyse(run, tmp_path).fail_reason == "exit_code=2"


@pytest.mark.parametrize("error_report", [False, True])
def test_stop_request_with_failing_event_is_stopped(tmp_path: Path, error_report: bool) -> None:
    run = _make_run(
        tmp_path, _step_fail(reason="exit_code=2", error=ERROR_LINE), error_report=error_report, stopped=True
    )
    report = analyse(run, tmp_path)
    assert report.failing_step == observability.STOPPED_STEP == "stopped"
    assert report.fail_reason == observability.STOPPED_ERROR == "stopped by bakar stop"


@pytest.mark.parametrize("error_report", [False, True])
def test_stop_request_with_nonzero_run_error_is_stopped(tmp_path: Path, error_report: bool) -> None:
    run = _make_run(
        tmp_path,
        {"event": "run_error", "error": "killed", "exit_code": 2},
        error_report=error_report,
        stopped=True,
    )
    report = analyse(run, tmp_path)
    assert report.failing_step == "stopped"
    assert report.fail_reason == "stopped by bakar stop"


@pytest.mark.parametrize("error_report", [False, True])
def test_stop_request_with_zero_exit_is_unchanged(tmp_path: Path, error_report: bool) -> None:
    run = _make_run(tmp_path, {"event": "run_end", "exit_code": 0}, error_report=error_report, stopped=True)
    report = analyse(run, tmp_path)
    assert report.fail_reason is None
    assert report.failing_step in (None, "kas_build")
    assert report.failing_step != "stopped"
    if not error_report:
        assert report.failing_step is None


def test_stop_request_without_failure_or_terminal_event_is_unchanged(tmp_path: Path) -> None:
    run = _make_run(tmp_path, {"event": "step_start", "step": "kas_build"}, stopped=True)
    report = analyse(run, tmp_path)
    assert report.failing_step is None
    assert report.fail_reason is None


def _triage_json(runner: _CliRunner, workspace: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    monkeypatch.chdir(workspace)
    result = runner.invoke(app, ["triage", "--json"])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def test_cli_json_stopped_run(runner: _CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_run(tmp_path, _step_fail(reason="exit_code=2", error=ERROR_LINE), stopped=True)
    doc = _triage_json(runner, tmp_path, monkeypatch)
    assert doc["failing_step"] == "stopped"
    assert doc["fail_reason"] == "stopped by bakar stop"


def test_cli_json_error_line_run(runner: _CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_run(tmp_path, _step_fail(reason="exit_code=2", error=ERROR_LINE))
    doc = _triage_json(runner, tmp_path, monkeypatch)
    assert doc["failing_step"] == "kas_build"
    assert doc["fail_reason"] == ERROR_LINE
    assert set(doc) == {"version", "run_id", "failing_step", "fail_reason", "recipe_log", "suggestions"}


def test_cli_text_stopped_run(runner: _CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_run(tmp_path, _step_fail(reason="exit_code=2", error=ERROR_LINE), stopped=True)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["triage"])
    assert result.exit_code == 0, result.output
    assert "stopped by bakar stop" in result.output
    assert "step kas_build failed" not in result.output


def test_cli_structured_failure_keeps_null_reason(
    runner: _CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = _make_run(tmp_path, _step_fail(reason="exit_code=2", error=ERROR_LINE), stopped=True)
    (run / "bitbake-events.json").write_text(
        json.dumps({"failures": [{"recipe": "linux-imx", "task": "do_compile", "logfile": None}]})
    )
    doc = _triage_json(runner, tmp_path, monkeypatch)
    assert doc["failing_step"] == "linux-imx:do_compile"
    assert doc["fail_reason"] is None
