"""_build_progress elapsed span ends at the run's terminal record."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

import pytest

from bakar.commands import monitor

if TYPE_CHECKING:
    from pathlib import Path

RUN_ID = "20261020-202242-4043679"
START = "2026-10-20T20:22:42Z"


def _make_run(tmp_path: Path, records: list[dict], name: str = RUN_ID) -> Path:
    run = tmp_path / name
    run.mkdir()
    (run / "events.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records))
    return run


def _patch(monkeypatch: pytest.MonkeyPatch, *, build: bool = False, frontend: bool = False) -> None:
    monkeypatch.setattr(monitor, "is_build_running", lambda _d: (build, 1 if build else None, build))
    monkeypatch.setattr(monitor, "frontend_alive", lambda _d: frontend)


def _clock(monkeypatch: pytest.MonkeyPatch, base: float) -> list[float]:
    now = [base]
    monkeypatch.setattr(time, "time", lambda: now[0])
    return now


def test_finished_run_ends_at_run_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run = _make_run(
        tmp_path,
        [
            {"ts": START, "event": "run_start"},
            {"ts": "2026-10-20T20:41:27Z", "event": "run_end", "exit_code": 0},
        ],
    )
    _patch(monkeypatch)
    now = _clock(monkeypatch, 2_000_000_000.0)
    first = monitor._build_progress(run)["elapsed_seconds"]
    now[0] += 20
    second = monitor._build_progress(run)["elapsed_seconds"]
    assert first == pytest.approx(1125, abs=1)
    assert second == first


def test_no_terminal_not_live_uses_last_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run = _make_run(
        tmp_path,
        [
            {"ts": START, "event": "run_start"},
            {"ts": "2026-10-20T20:32:42Z", "event": "step"},
        ],
    )
    _patch(monkeypatch)
    now = _clock(monkeypatch, 2_000_000_000.0)
    first = monitor._build_progress(run)["elapsed_seconds"]
    now[0] += 20
    second = monitor._build_progress(run)["elapsed_seconds"]
    assert first == pytest.approx(600, abs=1)
    assert second == first


def test_frontend_alive_grows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run = _make_run(tmp_path, [{"ts": START, "event": "run_start"}])
    _patch(monkeypatch, frontend=True)
    now = _clock(monkeypatch, 2_000_000_000.0)
    first = monitor._build_progress(run)["elapsed_seconds"]
    now[0] += 20
    assert monitor._build_progress(run)["elapsed_seconds"] == pytest.approx(first + 20)


def test_live_build_grows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run = _make_run(tmp_path, [{"ts": START, "event": "run_start"}])
    _patch(monkeypatch, build=True)
    now = _clock(monkeypatch, 2_000_000_000.0)
    first = monitor._build_progress(run)["elapsed_seconds"]
    now[0] += 20
    assert monitor._build_progress(run)["elapsed_seconds"] == pytest.approx(first + 20)


def test_unparseable_run_name_is_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run = _make_run(tmp_path, [{"ts": START, "event": "run_start"}], name="not-a-run-id")
    _patch(monkeypatch)
    assert monitor._build_progress(run)["elapsed_seconds"] is None


@pytest.mark.parametrize(
    "records",
    [
        [],
        [{"event": "run_start"}, {"ts": "not-a-timestamp", "event": "step"}],
    ],
    ids=["no-records", "no-parseable-ts"],
)
def test_no_terminal_no_parseable_record_not_live_is_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, records: list[dict]
) -> None:
    run = _make_run(tmp_path, records)
    _patch(monkeypatch)
    now = _clock(monkeypatch, 2_000_000_000.0)
    first = monitor._build_progress(run)["elapsed_seconds"]
    now[0] += 20
    second = monitor._build_progress(run)["elapsed_seconds"]
    assert first is None
    assert second is None
