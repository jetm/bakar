"""Tests for ``bakar status``: state precedence, resolution and the JSON contract."""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

import bakar.commands.status as status_cmd
from bakar.commands._app import app

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit

UNIT = "bakar-detach-20261019-120000-abc123"
RUN_ID = "20261019-120001-4242"
JSON_KEYS = {
    "run_id",
    "unit",
    "host",
    "state",
    "exit_code",
    "run_dir",
    "log",
    "tasks_done",
    "tasks_total",
    "tasks_running",
    "elapsed_seconds",
    "failure",
}


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Runtime dir for unit records, one runs dir for foreground discovery, probes off."""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    runs = tmp_path / "ws" / "build" / "runs"
    runs.mkdir(parents=True)
    # cwd inside a workspace, so no-handle tests read the workspace run unless
    # a test moves outside it.
    (tmp_path / "ws" / ".bakar.toml").write_text("")
    monkeypatch.chdir(tmp_path / "ws")
    monkeypatch.setattr(
        status_cmd, "_resolve_triage_dirs", lambda _kas, _ws: ([(runs, "generic")], tmp_path, "label", tmp_path)
    )
    monkeypatch.setattr(status_cmd, "_unit_active", lambda _u: False)
    monkeypatch.setattr(status_cmd, "is_build_running", lambda _rd: (False, None, False))
    monkeypatch.setattr(status_cmd, "_frontend_alive", lambda _rd: False)
    return tmp_path


def _run_dir(env: Path, run_id: str = RUN_ID) -> Path:
    run = env / "ws" / "build" / "runs" / run_id
    run.mkdir(parents=True)
    return run


def _terminal(run: Path, code: int | None, event: str = "run_end") -> None:
    rec: dict[str, object] = {"event": event}
    if code is not None:
        rec["exit_code"] = code
    (run / "events.jsonl").write_text(json.dumps(rec) + "\n")


def _register(env: Path, run: Path, unit: str = UNIT) -> None:
    (env / "runtime" / f"{unit}.runs").write_text(
        json.dumps({"run_id": run.name, "run_dir": str(run), "pid": 1}) + "\n"
    )


def _sentinel(env: Path, code: int, unit: str = UNIT) -> None:
    (env / "runtime" / f"{unit}.log.rc").write_text(f"{code}\n")


def test_success_requires_a_recorded_zero_exit(env: Path) -> None:
    run = _run_dir(env)
    rec = status_cmd.compute_status(run.name)
    assert rec is not None
    assert rec.state == "unknown"
    assert rec.exit_code is None

    _terminal(run, 0)
    rec = status_cmd.compute_status(run.name)
    assert rec is not None
    assert (rec.state, rec.exit_code) == ("succeeded", 0)


def test_terminal_event_without_exit_code_is_unknown(env: Path) -> None:
    run = _run_dir(env)
    _terminal(run, None, event="run_error")
    rec = status_cmd.compute_status(run.name)
    assert rec is not None
    assert rec.state == "unknown"


def test_foreground_nonzero_terminal_event_fails(env: Path) -> None:
    run = _run_dir(env)
    _terminal(run, 1, event="run_error")
    rec = status_cmd.compute_status(run.name)
    assert rec is not None
    assert (rec.state, rec.exit_code) == ("failed", 1)


def test_sentinel_two_is_failed(env: Path) -> None:
    run = _run_dir(env)
    _register(env, run)
    _sentinel(env, 2)
    rec = status_cmd.compute_status(UNIT)
    assert rec is not None
    assert (rec.state, rec.exit_code, rec.run_id, rec.unit) == ("failed", 2, run.name, UNIT)


def test_sentinel_zero_is_succeeded(env: Path) -> None:
    run = _run_dir(env)
    _register(env, run)
    _sentinel(env, 0)
    rec = status_cmd.compute_status(UNIT)
    assert rec is not None
    assert (rec.state, rec.exit_code) == ("succeeded", 0)


def test_sentinel_beats_terminal_event(env: Path) -> None:
    run = _run_dir(env)
    _register(env, run)
    _terminal(run, 0)
    _sentinel(env, 3)
    rec = status_cmd.compute_status(UNIT)
    assert rec is not None
    assert (rec.state, rec.exit_code) == ("failed", 3)


def test_detached_run_id_resolves_through_registry(env: Path) -> None:
    run = _run_dir(env)
    _register(env, run)
    _sentinel(env, 0)
    rec = status_cmd.compute_status(run.name)
    assert rec is not None
    assert (rec.unit, rec.run_id) == (UNIT, run.name)


def test_starting_when_unit_active_without_a_run_id(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(status_cmd, "_unit_active", lambda _u: True)
    rec = status_cmd.compute_status(UNIT)
    assert rec is not None
    assert (rec.state, rec.run_id, rec.run_dir) == ("starting", None, None)
    assert rec.tasks_done is None
    assert rec.tasks_total is None


def test_running_when_unit_active_with_a_run(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run = _run_dir(env)
    _register(env, run)
    monkeypatch.setattr(status_cmd, "_unit_active", lambda _u: True)
    rec = status_cmd.compute_status(UNIT)
    assert rec is not None
    assert rec.state == "running"


def test_running_when_foreground_build_process_is_live(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run = _run_dir(env)
    monkeypatch.setattr(status_cmd, "is_build_running", lambda _rd: (True, 99, True))
    rec = status_cmd.compute_status(run.name)
    assert rec is not None
    assert rec.state == "running"


def test_liveness_never_overrides_a_recorded_exit(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run = _run_dir(env)
    _terminal(run, 1)
    monkeypatch.setattr(status_cmd, "is_build_running", lambda _rd: (True, 99, True))
    rec = status_cmd.compute_status(run.name)
    assert rec is not None
    assert rec.state == "failed"


def test_no_handle_picks_the_newest_run_not_the_newest_failure(env: Path) -> None:
    old = _run_dir(env, "20261019-100000-1")
    _terminal(old, 1)
    new = _run_dir(env, "20261019-110000-2")
    _terminal(new, 0)
    rec = status_cmd.compute_status(None)
    assert rec is not None
    assert (rec.run_id, rec.state) == (new.name, "succeeded")


def test_unknown_handle_resolves_to_nothing(env: Path) -> None:
    assert status_cmd.compute_status("no-such-run") is None


def test_unknown_handle_exits_two_and_names_it(env: Path) -> None:
    result = CliRunner().invoke(app, ["status", "no-such-run"])
    assert result.exit_code == 2
    assert "no-such-run" in result.output


def test_unregistered_unit_name_that_is_gone_exits_two(env: Path) -> None:
    result = CliRunner().invoke(app, ["status", UNIT])
    assert result.exit_code == 2


def test_null_totals_are_null_not_zero(env: Path) -> None:
    run = _run_dir(env)
    rec = status_cmd.compute_status(run.name)
    assert rec is not None
    assert rec.tasks_total is None
    assert rec.tasks_done is None
    assert rec.tasks_running is None
    assert rec.host is None


def test_failed_run_names_recipe_and_task(env: Path) -> None:
    run = _run_dir(env)
    _terminal(run, 1)
    artifact = {
        "schema_version": 1,
        "build": {"outcome": "failed"},
        "tasks": [],
        "setscene": {},
        "failures": [{"recipe": "openssl", "task": "do_compile"}],
    }
    (run / "bitbake-events.json").write_text(json.dumps(artifact))
    rec = status_cmd.compute_status(run.name)
    assert rec is not None
    assert rec.failure == {"recipe": "openssl", "task": "do_compile", "step": None, "error": None}


def _events(run: Path, *events: dict[str, object]) -> None:
    (run / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))


def test_failure_before_any_task_comes_from_the_first_step_fail(env: Path) -> None:
    run = _run_dir(env)
    _events(
        run,
        {"event": "step_start", "step": "kas_shell_live"},
        {"event": "step_fail", "step": "kas_shell_live", "reason": "exit_code=1", "exit_code": 1},
        {"event": "step_fail", "step": "later", "reason": "second"},
        {"event": "run_error", "error": "boom", "error_type": "Exit", "exit_code": 1},
    )
    rec = status_cmd.compute_status(run.name)
    assert rec is not None
    assert rec.state == "failed"
    assert rec.failure == {"recipe": None, "task": None, "step": "kas_shell_live", "error": "exit_code=1"}


def test_step_fail_error_text_beats_reason(env: Path) -> None:
    run = _run_dir(env)
    _events(
        run,
        {"event": "step_fail", "step": "gen_kas", "reason": "r", "error": "kas file missing"},
        {"event": "run_error", "error": "", "error_type": "Exit", "exit_code": 2},
    )
    rec = status_cmd.compute_status(run.name)
    assert rec is not None
    assert rec.failure == {"recipe": None, "task": None, "step": "gen_kas", "error": "kas file missing"}


def test_failure_from_a_bare_run_error(env: Path) -> None:
    run = _run_dir(env)
    _events(run, {"event": "run_error", "error": "no such target", "error_type": "BakarError", "exit_code": 2})
    rec = status_cmd.compute_status(run.name)
    assert rec is not None
    assert rec.failure == {"recipe": None, "task": None, "step": "BakarError", "error": "no such target"}


def test_run_error_with_empty_text_keeps_error_null(env: Path) -> None:
    run = _run_dir(env)
    _events(run, {"event": "run_error", "error": "", "error_type": "Exit", "exit_code": 1})
    rec = status_cmd.compute_status(run.name)
    assert rec is not None
    assert rec.failure == {"recipe": None, "task": None, "step": "Exit", "error": None}


def test_failed_run_with_no_failure_event_has_no_failure(env: Path) -> None:
    run = _run_dir(env)
    _terminal(run, 1)
    rec = status_cmd.compute_status(run.name)
    assert rec is not None
    assert rec.failure is None


def test_failure_is_absent_unless_failed(env: Path) -> None:
    run = _run_dir(env)
    _terminal(run, 0)
    artifact = {
        "schema_version": 1,
        "build": {},
        "tasks": [],
        "setscene": {},
        "failures": [{"recipe": "x", "task": "y"}],
    }
    (run / "bitbake-events.json").write_text(json.dumps(artifact))
    rec = status_cmd.compute_status(run.name)
    assert rec is not None
    assert rec.failure is None


def test_json_stdout_carries_only_the_payload(env: Path) -> None:
    run = _run_dir(env)
    _terminal(run, 1)
    result = CliRunner().invoke(app, ["status", run.name, "--json"])
    assert result.exit_code == 0
    assert result.stderr == ""
    lines = result.stdout.splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert set(payload) == JSON_KEYS
    assert len(payload) == 12
    assert payload["state"] == "failed"


def test_summary_goes_to_stderr_without_json(env: Path) -> None:
    run = _run_dir(env)
    _terminal(run, 0)
    result = CliRunner().invoke(app, ["status", run.name])
    assert result.exit_code == 0
    assert result.stdout == ""
    assert "succeeded" in result.stderr


def test_sentinel_written_between_read_and_liveness_probe_is_seen(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run = _run_dir(env)
    _register(env, run)

    def probe(_u: str) -> bool:
        _sentinel(env, 2)
        return False

    monkeypatch.setattr(status_cmd, "_unit_active", probe)
    rec = status_cmd.compute_status(UNIT)
    assert rec is not None
    assert (rec.state, rec.exit_code) == ("failed", 2)


def test_unregistered_unit_finishing_during_the_probe_is_not_dropped(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def probe(_u: str) -> bool:
        _sentinel(env, 2)
        return False

    monkeypatch.setattr(status_cmd, "_unit_active", probe)
    rec = status_cmd.compute_status(UNIT)
    assert rec is not None
    assert (rec.state, rec.exit_code) == ("failed", 2)


def test_no_handle_uses_the_registered_unit_liveness(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run = _run_dir(env)
    _register(env, run)
    monkeypatch.setattr(status_cmd, "_unit_active", lambda _u: True)
    rec = status_cmd.compute_status(None)
    assert rec is not None
    assert (rec.state, rec.unit) == ("running", UNIT)


def test_per_run_terminal_event_does_not_end_an_active_unit(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first = _run_dir(env, "20261019-120001-1")
    second = _run_dir(env, "20261019-120002-2")
    _terminal(first, 0)
    (env / "runtime" / f"{UNIT}.runs").write_text(
        "".join(json.dumps({"run_id": r.name, "run_dir": str(r), "pid": 1}) + "\n" for r in (first, second))
    )
    monkeypatch.setattr(status_cmd, "_unit_active", lambda _u: True)
    for handle in (first.name, second.name, UNIT):
        rec = status_cmd.compute_status(handle)
        assert rec is not None
        assert rec.state == "running"
    _sentinel(env, 0)
    rec = status_cmd.compute_status(first.name)
    assert rec is not None
    assert rec.state == "succeeded"


def _register_two(env: Path) -> tuple[Path, Path]:
    first = _run_dir(env, "20261019-120001-1")
    second = _run_dir(env, "20261019-120002-2")
    (env / "runtime" / f"{UNIT}.runs").write_text(
        "".join(json.dumps({"run_id": r.name, "run_dir": str(r), "pid": 1}) + "\n" for r in (first, second))
    )
    return first, second


def test_inactive_unit_without_sentinel_is_unknown_not_a_run_outcome(env: Path) -> None:
    first, _second = _register_two(env)
    _terminal(first, 0)
    rec = status_cmd.compute_status(UNIT)
    assert rec is not None
    assert (rec.state, rec.exit_code) == ("unknown", None)


def test_ended_unit_run_handles_report_their_own_exit(env: Path) -> None:
    first, second = _register_two(env)
    _terminal(first, 0)
    _terminal(second, 1)
    _sentinel(env, 1)
    a = status_cmd.compute_status(first.name)
    b = status_cmd.compute_status(second.name)
    u = status_cmd.compute_status(UNIT)
    assert a is not None
    assert b is not None
    assert u is not None
    assert (a.state, a.exit_code) == ("succeeded", 0)
    assert (b.state, b.exit_code) == ("failed", 1)
    assert (u.state, u.exit_code) == ("failed", 1)


def test_ended_unit_run_without_terminal_event_falls_back_to_the_sentinel(env: Path) -> None:
    first, second = _register_two(env)
    _terminal(second, 0)
    _sentinel(env, 1)
    rec = status_cmd.compute_status(first.name)
    assert rec is not None
    assert (rec.state, rec.exit_code) == ("failed", 1)


def test_stale_build_pid_without_matching_cmdline_is_not_running(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run = _run_dir(env)
    monkeypatch.setattr(status_cmd, "is_build_running", lambda _rd: (True, 99, False))
    rec = status_cmd.compute_status(run.name)
    assert rec is not None
    assert rec.state == "unknown"


def test_live_frontend_keeps_a_run_running_between_build_steps(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run = _run_dir(env)
    monkeypatch.setattr(status_cmd, "_frontend_alive", lambda _rd: True)
    rec = status_cmd.compute_status(run.name)
    assert rec is not None
    assert rec.state == "running"


def test_live_frontend_never_overrides_a_recorded_exit(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run = _run_dir(env)
    _terminal(run, 0)
    monkeypatch.setattr(status_cmd, "_frontend_alive", lambda _rd: True)
    rec = status_cmd.compute_status(run.name)
    assert rec is not None
    assert rec.state == "succeeded"


def test_frontend_pid_comes_from_the_last_hyphen_segment() -> None:
    assert status_cmd._frontend_pid("20261019-120001-4242") == 4242
    assert status_cmd._frontend_pid("nohyphens") is None
    assert status_cmd._frontend_pid("20261019-120001-x") is None


def test_frontend_alive_requires_a_bakar_cmdline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    real_read = status_cmd.Path.read_bytes
    cmdlines = {os.getpid(): b"/usr/bin/python\x00bakar\x00build\x00"}

    def fake_read(self: Path) -> bytes:
        parts = self.parts
        if len(parts) == 4 and parts[1] == "proc":
            try:
                return cmdlines[int(parts[2])]
            except KeyError:
                raise FileNotFoundError from None
        return real_read(self)

    monkeypatch.setattr(status_cmd.Path, "read_bytes", fake_read)
    assert status_cmd._frontend_alive(tmp_path / f"20261019-120001-{os.getpid()}") is True
    cmdlines[os.getpid()] = b"/usr/bin/sleep\x00 1\x00"
    assert status_cmd._frontend_alive(tmp_path / f"20261019-120001-{os.getpid()}") is False
    assert status_cmd._frontend_alive(tmp_path / "20261019-120001-999999999") is False


def test_unusable_runtime_dir_reports_unresolved_with_a_reason(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", "relative/dir")
    result = CliRunner().invoke(app, ["status", UNIT])
    assert result.exit_code == 2
    assert "XDG_RUNTIME_DIR" in result.stderr


OLDER_UNIT = "bakar-dispatch-20261019-110000-aaaaaa"
NEWER_UNIT = "bakar-dispatch-20261019-100000-bbbbbb"


def _outside(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    outside = env / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)


def _finished_unit(env: Path, unit: str, mtime: float) -> None:
    _sentinel(env, 0, unit=unit)
    os.utime(env / "runtime" / f"{unit}.log.rc", (mtime, mtime))


def test_no_handle_outside_a_workspace_reports_the_newest_unit(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _outside(env, monkeypatch)
    _finished_unit(env, OLDER_UNIT, 1_000_000)
    _finished_unit(env, NEWER_UNIT, 2_000_000)
    rec = status_cmd.compute_status(None)
    assert rec is not None
    assert (rec.unit, rec.state) == (NEWER_UNIT, "succeeded")


def test_no_handle_inside_a_workspace_keeps_the_workspace_run(env: Path) -> None:
    run = _run_dir(env)
    _terminal(run, 0)
    _finished_unit(env, NEWER_UNIT, 4_000_000_000)
    rec = status_cmd.compute_status(None)
    assert rec is not None
    assert (rec.run_id, rec.unit) == (run.name, None)


def test_no_handle_with_a_workspace_option_does_not_fall_back(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run = _run_dir(env)
    _terminal(run, 0)
    _outside(env, monkeypatch)
    _finished_unit(env, NEWER_UNIT, 4_000_000_000)
    rec = status_cmd.compute_status(None, workspace=env / "ws")
    assert rec is not None
    assert (rec.run_id, rec.unit) == (run.name, None)


def test_no_handle_outside_a_workspace_without_units_resolves_to_nothing(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _outside(env, monkeypatch)
    assert status_cmd.compute_status(None) is None


def test_cli_no_handle_outside_a_workspace_reads_the_newest_unit(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _outside(env, monkeypatch)
    missing = CliRunner().invoke(app, ["status", "--json"])
    assert missing.exit_code == 2
    assert "No run found for the current workspace" in missing.output

    _finished_unit(env, OLDER_UNIT, 1_000_000)
    _finished_unit(env, NEWER_UNIT, 2_000_000)
    result = CliRunner().invoke(app, ["status", "--json"])
    assert result.exit_code == 0
    assert "Not inside a BSP workspace" not in result.output
    payload = json.loads(result.stdout)
    assert set(payload) == JSON_KEYS
    assert (payload["unit"], payload["state"]) == (NEWER_UNIT, "succeeded")
