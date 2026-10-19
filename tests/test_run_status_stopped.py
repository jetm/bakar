"""Tests for the ``bakar status`` failure object on a stopped run."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import bakar.commands.status as status_cmd
import bakar.commands.wait as wait_cmd
from bakar import observability
from tests.test_run_status import _run_dir, _terminal, env  # noqa: F401

pytestmark = pytest.mark.unit

ERROR_TEXT = "ERROR: Nothing PROVIDES 'does-not-exist'"


def _append(run: Path, rec: dict[str, object]) -> None:
    with (run / "events.jsonl").open("a") as fh:
        fh.write(json.dumps(rec) + "\n")


def _failed_run(env: Path, **fail: object) -> Path:  # noqa: F811
    run = _run_dir(env)
    _append(run, {"ts": "2026-10-19T12:00:00Z", "event": "step_fail", "step": "kas_build", **fail})
    _append(run, {"event": "run_end", "exit_code": 2})
    return run


def _failure(run: Path) -> dict[str, str | None] | None:
    rec = status_cmd.compute_status(run.name)
    assert rec is not None
    return rec.failure


def test_stop_request_reports_stopped(env: Path) -> None:  # noqa: F811
    run = _failed_run(env, reason="exit_code=2", error=ERROR_TEXT)
    assert observability.write_stop_request(run)
    rec = status_cmd.compute_status(run.name)
    assert rec is not None
    assert (rec.state, rec.exit_code) == ("failed", 2)
    assert rec.failure == {"recipe": None, "task": None, "step": "stopped", "error": "stopped by bakar stop"}


def test_without_request_reports_step_error(env: Path) -> None:  # noqa: F811
    run = _failed_run(env, reason="exit_code=2", error=ERROR_TEXT)
    assert _failure(run) == {"recipe": None, "task": None, "step": "kas_build", "error": ERROR_TEXT}


def test_reason_only_step(env: Path) -> None:  # noqa: F811
    run = _failed_run(env, reason="exit_code=2")
    assert _failure(run) == {"recipe": None, "task": None, "step": "kas_build", "error": "exit_code=2"}


def test_structured_task_failure_beats_stop_request(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    run = _failed_run(env, reason="exit_code=2")
    observability.write_stop_request(run)
    monkeypatch.setattr(
        status_cmd, "_read_structured_failures", lambda _rd: [{"recipe": "busybox", "task": "do_compile"}]
    )
    failure = _failure(run)
    assert failure is not None
    assert (failure["recipe"], failure["task"]) == ("busybox", "do_compile")


def test_stop_request_on_zero_exit_stays_succeeded(env: Path) -> None:  # noqa: F811
    run = _run_dir(env)
    _terminal(run, 0)
    observability.write_stop_request(run)
    rec = status_cmd.compute_status(run.name)
    assert rec is not None
    assert (rec.state, rec.failure) == ("succeeded", None)


def test_wait_exits_one_for_stopped_run(env: Path) -> None:  # noqa: F811
    run = _failed_run(env, reason="exit_code=2")
    observability.write_stop_request(run)
    code, rec = wait_cmd.run_wait(
        run.name,
        timeout=5,
        interval=1,
        emit=lambda _line: None,
        status_fn=lambda target, workspace=None: status_cmd.compute_status(target, workspace=workspace),
    )
    assert code == 1
    assert rec is not None
    assert rec.failure is not None
    assert rec.failure["step"] == "stopped"


def test_kas_log_is_never_read(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    run = _failed_run(env, reason="exit_code=2")
    (run / "kas.log").write_text("ERROR: other\n")
    touched: list[str] = []
    real_read_text, real_open = Path.read_text, Path.open

    def guarded_read_text(self: Path, *args: object, **kwargs: object) -> str:
        if self.name == "kas.log":
            touched.append("read_text")
        return real_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

    def guarded_open(self: Path, *args: object, **kwargs: object) -> object:
        if self.name == "kas.log":
            touched.append("open")
        return real_open(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", guarded_read_text)
    monkeypatch.setattr(Path, "open", guarded_open)
    assert _failure(run) == {"recipe": None, "task": None, "step": "kas_build", "error": "exit_code=2"}
    assert touched == []
