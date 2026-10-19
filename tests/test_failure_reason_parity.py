"""Cross-reader parity: ``bakar status``, ``bakar triage`` and ``bakar wait`` agree on why a run failed."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

import bakar.commands.status as status_cmd
from bakar import observability
from bakar.commands._app import app

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit

RUN_ID = "20261019-120001"
ERROR_LINE = "ERROR: Nothing PROVIDES 'does-not-exist'"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """One workspace whose runs dir both status and triage resolve; probes off."""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / ".bakar.toml").write_text("")
    runs = ws / "nxp" / "build" / "runs"
    runs.mkdir(parents=True)
    monkeypatch.setattr(
        status_cmd, "_resolve_triage_dirs", lambda _kas, _ws: ([(runs, "nxp")], tmp_path, "label", tmp_path)
    )
    monkeypatch.setattr(status_cmd, "_unit_active", lambda _u: False)
    monkeypatch.setattr(status_cmd, "is_build_running", lambda _rd: (False, None, False))
    monkeypatch.setattr(status_cmd, "_frontend_alive", lambda _rd: False)
    monkeypatch.chdir(ws)
    return ws


def _step_fail(**extra: object) -> dict[str, object]:
    return {"event": "step_fail", "step": "kas_build", "exit_code": 2, **extra}


def _build(env: Path, *events: dict[str, object], stopped: bool = False) -> Path:
    run = env / "nxp" / "build" / "runs" / RUN_ID
    run.mkdir(parents=True)
    (run / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    (run / "kas.log").write_text("NOTE: nothing\n")
    if stopped:
        assert observability.write_stop_request(run)
    return run


def _status(run: Path) -> status_cmd.StatusRecord:
    rec = status_cmd.compute_status(run.name)
    assert rec is not None
    return rec


def _triage(runner: CliRunner) -> dict[str, object]:
    result = runner.invoke(app, ["triage", RUN_ID, "--json"])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


RUN_ERROR = {"event": "run_error", "exit_code": 2}


def _stopped_with_error(env: Path) -> Path:
    return _build(env, _step_fail(reason="exit_code=2", error=ERROR_LINE), RUN_ERROR, stopped=True)


def _error_line(env: Path) -> Path:
    return _build(env, _step_fail(reason="exit_code=2", error=ERROR_LINE), RUN_ERROR)


def _reason_only(env: Path) -> Path:
    return _build(env, _step_fail(reason="exit_code=2"), RUN_ERROR)


def _no_step_fail_stopped(env: Path) -> Path:
    return _build(env, RUN_ERROR, stopped=True)


def _structured(env: Path) -> Path:
    run = _build(env, _step_fail(reason="exit_code=2", error=ERROR_LINE), RUN_ERROR, stopped=True)
    (run / "bitbake-events.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "build": {"outcome": "failed"},
                "tasks": [],
                "setscene": {},
                "failures": [{"recipe": "linux-imx", "task": "do_compile", "logfile": None}],
            }
        )
    )
    return run


@pytest.mark.parametrize(
    ("builder", "step", "error"),
    [
        (_stopped_with_error, "stopped", "stopped by bakar stop"),
        (_error_line, "kas_build", ERROR_LINE),
        (_reason_only, "kas_build", "exit_code=2"),
        (_no_step_fail_stopped, "stopped", "stopped by bakar stop"),
    ],
    ids=["stopped-with-error", "error-line", "reason-only", "stopped-no-step-fail"],
)
def test_status_and_triage_name_the_same_reason(env: Path, builder, step: str, error: str) -> None:
    run = builder(env)
    rec = _status(run)
    assert rec.state == "failed"
    assert rec.failure is not None
    assert rec.failure["step"] == step
    assert rec.failure["error"] == error

    doc = _triage(CliRunner())
    assert doc["fail_reason"] == rec.failure["error"]
    assert doc["failing_step"] == rec.failure["step"]


def test_structured_failure_is_named_by_status_and_null_in_triage(env: Path) -> None:
    run = _structured(env)
    rec = _status(run)
    assert rec.state == "failed"
    assert rec.failure is not None
    assert (rec.failure["recipe"], rec.failure["task"]) == ("linux-imx", "do_compile")

    doc = _triage(CliRunner())
    assert doc["fail_reason"] is None


@pytest.mark.parametrize(
    "builder",
    [_stopped_with_error, _error_line, _reason_only, _structured, _no_step_fail_stopped],
    ids=["stopped-with-error", "error-line", "reason-only", "structured", "stopped-no-step-fail"],
)
def test_wait_exits_1_for_every_failed_run(env: Path, builder) -> None:
    builder(env)
    result = CliRunner().invoke(app, ["wait", RUN_ID, "--timeout", "5"])
    assert result.exit_code == 1, result.output


def test_stop_request_does_not_make_a_zero_exit_run_fail(env: Path) -> None:
    run = _build(env, {"event": "run_end", "exit_code": 0}, stopped=True)
    rec = _status(run)
    assert rec.state == "succeeded"
    assert rec.failure is None

    doc = _triage(CliRunner())
    assert doc["fail_reason"] is None
