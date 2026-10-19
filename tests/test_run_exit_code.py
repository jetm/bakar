"""``RunLogger`` records the front-end exit code on every terminal event."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
import typer

from bakar.observability import RunLogger, last_run_event
from bakar.report import _duration_s

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


def _run(tmp_path: Path, exc: BaseException | None) -> list[dict]:
    log = RunLogger(tmp_path)
    if exc is None:
        with log:
            pass
    else:
        # __exit__ must not swallow: a True return would make pytest.raises fail.
        with pytest.raises(type(exc)), log:
            raise exc
    return [json.loads(line) for line in log.events_path.read_text().splitlines()]


def test_clean_exit_records_run_end_zero(tmp_path: Path) -> None:
    events = _run(tmp_path, None)
    assert [e["event"] for e in events] == ["run_start", "run_end"]
    assert events[-1]["exit_code"] == 0


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (typer.Exit(code=0), 0),
        (typer.Exit(code=3), 3),
        (SystemExit(2), 2),
        (SystemExit(None), 0),
        (SystemExit("boom"), 1),
        (SystemExit(True), 1),
        (SystemExit(False), 0),
        (KeyboardInterrupt(), 130),
        (ValueError("x"), 1),
    ],
)
def test_exception_exit_codes(tmp_path: Path, exc: BaseException, expected: int) -> None:
    events = _run(tmp_path, exc)
    assert events[-1]["event"] == "run_error"
    assert events[-1]["exit_code"] == expected
    assert events[-1]["error_type"] == type(exc).__name__


def test_report_finds_terminal_event_with_duration(tmp_path: Path) -> None:
    _run(tmp_path, typer.Exit(code=3))
    events_path = next(tmp_path.glob("*/events.jsonl"))
    start = last_run_event(events_path, lambda r: r.get("event") == "run_start")
    end = last_run_event(events_path, lambda r: r.get("event") in ("run_end", "run_error"))
    assert end is not None
    assert end["exit_code"] == 3
    assert _duration_s(start, end) is not None
