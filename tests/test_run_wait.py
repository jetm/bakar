"""Tests for ``bakar wait``: exit statuses, timeout bound, stream split, interval floor."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from typer.testing import CliRunner

import bakar.commands.wait as wait_cmd
from bakar.commands._app import app
from bakar.commands.status import StatusRecord

pytestmark = pytest.mark.unit


def _rec(state: str, **kw: object) -> StatusRecord:
    base: dict[str, object] = {
        "run_id": "R1",
        "unit": None,
        "host": None,
        "state": state,
        "exit_code": None,
        "run_dir": None,
        "log": None,
        "tasks_done": None,
        "tasks_total": None,
        "tasks_running": None,
        "elapsed_seconds": None,
        "failure": None,
    }
    base.update(kw)
    return StatusRecord(**base)  # type: ignore[arg-type]


class Clock:
    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


def _script(monkeypatch: pytest.MonkeyPatch, records: list[StatusRecord | None]) -> list[int]:
    """compute_status yields ``records`` in order, then repeats the last."""
    calls: list[int] = []

    def fake(handle: str | None, *, workspace: object = None) -> StatusRecord | None:
        calls.append(1)
        return records[min(len(calls) - 1, len(records) - 1)]

    monkeypatch.setattr(wait_cmd, "compute_status", fake)
    return calls


def _run(
    clock: Clock, lines: list[str], *, timeout: float | None = None, interval: float = 30
) -> tuple[int, StatusRecord | None]:
    return wait_cmd.run_wait(
        "R1",
        timeout=timeout,
        interval=interval,
        clock=clock,
        sleep=clock.sleep,
        now=lambda: datetime(2026, 10, 19, 12, 34, 56, tzinfo=UTC),
        emit=lines.append,
    )


@pytest.mark.parametrize(
    ("state", "code"),
    [("succeeded", 0), ("failed", 1), ("unknown", 3)],
)
def test_terminal_states_map_to_exit_codes(monkeypatch: pytest.MonkeyPatch, state: str, code: int) -> None:
    _script(monkeypatch, [_rec("running"), _rec(state)])
    clock = Clock()
    got, rec = _run(clock, [])
    assert got == code
    assert rec is not None
    assert rec.state == state
    assert clock.sleeps == [30]


def test_unresolved_handle_exits_2(monkeypatch: pytest.MonkeyPatch) -> None:
    _script(monkeypatch, [None])
    lines: list[str] = []
    got, rec = _run(Clock(), lines)
    assert got == 2
    assert rec is None
    assert "R1" in lines[-1]


def test_timeout_while_running_is_124_within_one_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    _script(monkeypatch, [_rec("running")])
    clock = Clock()
    got, _ = _run(clock, [], timeout=70, interval=30)
    assert got == 124
    assert 70 <= clock.t <= 70 + 30
    assert clock.sleeps == [30, 30, 10]


def test_timeout_while_starting_is_124(monkeypatch: pytest.MonkeyPatch) -> None:
    _script(monkeypatch, [_rec("starting")])
    got, _ = _run(Clock(), [], timeout=5, interval=30)
    assert got == 124


def test_no_timeout_waits_until_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _script(monkeypatch, [_rec("running")] * 50 + [_rec("succeeded")])
    clock = Clock()
    got, _ = _run(clock, [])
    assert got == 0
    assert len(calls) == 51
    assert clock.t == 50 * 30


@pytest.mark.parametrize("requested", [0, 0.2, -5])
def test_interval_is_clamped_to_one_second(monkeypatch: pytest.MonkeyPatch, requested: float) -> None:
    _script(monkeypatch, [_rec("running"), _rec("succeeded")])
    clock = Clock()
    _run(clock, [], interval=requested)
    assert clock.sleeps == [1.0]


def test_progress_line_format(monkeypatch: pytest.MonkeyPatch) -> None:
    _script(monkeypatch, [_rec("running", tasks_done=3, tasks_total=10, tasks_running=2), _rec("failed")])
    lines: list[str] = []
    _run(Clock(), lines)
    assert lines == ["12:34:56Z R1 running tasks=3/10 running=2", "12:34:56Z R1 failed tasks=?/? running=?"]


def test_no_handle_pins_the_target_after_the_first_poll() -> None:
    seen: list[str | None] = []
    answers = [_rec("running", run_id="X"), _rec("running", run_id="X"), _rec("succeeded", run_id="X")]

    def resolver(handle: str | None, *, workspace: object = None) -> StatusRecord | None:
        seen.append(handle)
        return answers[len(seen) - 1]

    clock = Clock()
    got, rec = wait_cmd.run_wait(
        None,
        timeout=None,
        interval=30,
        clock=clock,
        sleep=clock.sleep,
        now=lambda: datetime(2026, 10, 19, 12, 34, 56, tzinfo=UTC),
        emit=lambda _l: None,
        status_fn=resolver,
    )
    assert got == 0
    assert rec is not None
    assert seen == [None, "X", "X"]


def test_no_handle_pins_the_unit_when_the_run_is_registered() -> None:
    seen: list[str | None] = []
    unit = "bakar-detach-20261019-120000-abc123"
    answers = [_rec("running", unit=unit), _rec("failed", unit=unit)]

    def resolver(handle: str | None, *, workspace: object = None) -> StatusRecord | None:
        seen.append(handle)
        return answers[len(seen) - 1]

    clock = Clock()
    wait_cmd.run_wait(
        None,
        timeout=None,
        interval=30,
        clock=clock,
        sleep=clock.sleep,
        emit=lambda _l: None,
        status_fn=resolver,
    )
    assert seen == [None, unit]


def test_default_interval_is_30() -> None:
    assert wait_cmd.DEFAULT_INTERVAL == 30


def test_stdout_empty_without_json(monkeypatch: pytest.MonkeyPatch) -> None:
    _script(monkeypatch, [_rec("succeeded", exit_code=0)])
    result = CliRunner().invoke(app, ["wait", "R1"])
    assert result.exit_code == 0
    assert result.stdout == ""
    assert "R1 succeeded" in result.stderr


def test_json_is_the_only_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    _script(monkeypatch, [_rec("failed", exit_code=1)])
    result = CliRunner().invoke(app, ["wait", "R1", "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["state"] == "failed"
    assert payload["exit_code"] == 1
    assert "R1 failed" in result.stderr


def test_unresolved_with_json_prints_nothing_on_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    _script(monkeypatch, [None])
    result = CliRunner().invoke(app, ["wait", "nope", "--json"])
    assert result.exit_code == 2
    assert result.stdout == ""
    assert "nope" in result.stderr


@pytest.mark.parametrize("option", ["--interval", "--timeout"])
@pytest.mark.parametrize("value", ["nan", "inf"])
def test_non_finite_interval_and_timeout_are_refused(monkeypatch: pytest.MonkeyPatch, option: str, value: str) -> None:
    calls = _script(monkeypatch, [_rec("running")])
    result = CliRunner().invoke(app, ["wait", "R1", option, value])
    assert result.exit_code == 2
    assert option in result.output
    assert calls == []
