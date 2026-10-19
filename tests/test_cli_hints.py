"""Tests for the agent next-step hints in bakar.commands._hints."""

from __future__ import annotations

import io

import pytest
from rich.console import Console

from bakar.commands import _hints


@pytest.fixture
def streams(monkeypatch: pytest.MonkeyPatch) -> tuple[io.StringIO, io.StringIO]:
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    monkeypatch.setattr(_hints, "console", Console(file=err, width=40))
    return out, err


def test_hints_never_reach_stdout(streams: tuple[io.StringIO, io.StringIO]) -> None:
    out, err = streams
    _hints.emit(_hints.foreground_nudge() + _hints.after_finish("r1", 1) + _hints.after_detach("h", "r1"))
    assert out.getvalue() == ""
    assert "--detach" in err.getvalue()


def test_emit_keeps_long_lines_unwrapped(streams: tuple[io.StringIO, io.StringIO]) -> None:
    _, err = streams
    _hints.emit(["hint: `bakar wait some-handle --timeout 540 --on host` [x]"])
    assert err.getvalue().count("\n") == 1
    assert "[x]" in err.getvalue()


@pytest.mark.parametrize(
    ("value", "expected"),
    [("1", True), ("0", False), ("", False), ("true", False), (None, False)],
)
def test_agent_mode_only_for_one(value: str | None, expected: bool) -> None:
    environ = {} if value is None else {"CLAUDECODE": value}
    assert _hints.agent_mode(environ) is expected


def test_foreground_nudge_names_detach_and_wait() -> None:
    (line,) = _hints.foreground_nudge()
    assert "--detach" in line
    assert "bakar wait <run_id> --timeout 540" in line


def test_after_finish_by_rc() -> None:
    (bad,) = _hints.after_finish("r1", 2)
    (good,) = _hints.after_finish("r1", 0)
    assert "bakar triage r1 --json" in bad
    assert "report" not in bad
    assert "bakar report r1 --json" in good
    assert "triage" not in good


def test_after_detach_with_run_id() -> None:
    text = "\n".join(_hints.after_detach("h", "r1"))
    assert "bakar wait h --timeout 540" in text
    assert "bakar status h --json" in text
    assert "bakar stop <kas.yml> --run r1" in text
    assert "--on" not in text


def test_after_detach_without_run_id() -> None:
    text = "\n".join(_hints.after_detach("h", None))
    assert "bakar stop" not in text
    assert "`bakar status h --json` names the run id" in text


def test_after_detach_with_on_drops_run_from_stop() -> None:
    text = "\n".join(_hints.after_detach("h", "r1", on="pc3"))
    assert "bakar wait h --timeout 540 --on pc3" in text
    assert "bakar status h --json --on pc3" in text
    assert "bakar stop --on pc3" in text
    assert "--run" not in text
