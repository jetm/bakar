"""Run-end timestamp helpers and the front-end liveness helpers in observability."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from bakar import observability
from bakar.observability import frontend_alive, frontend_pid, last_record_at, run_ended_at

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _epoch(ts: str) -> float:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp()


def _write_events(run_dir: Path, records: list[dict]) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "events.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records))
    return run_dir


def test_run_ended_at_returns_the_last_run_end(tmp_path: Path) -> None:
    run_dir = _write_events(
        tmp_path / "run",
        [
            {"ts": "2026-10-20T20:22:42Z", "event": "run_start"},
            {"ts": "2026-10-20T20:30:00Z", "event": "run_end"},
            {"ts": "2026-10-20T20:35:00Z", "event": "step_start"},
            {"ts": "2026-10-20T20:41:27Z", "event": "run_end"},
        ],
    )
    assert run_ended_at(run_dir) == _epoch("2026-10-20T20:41:27Z")


def test_run_ended_at_returns_a_run_error_record(tmp_path: Path) -> None:
    run_dir = _write_events(
        tmp_path / "run",
        [
            {"ts": "2026-10-20T20:22:42Z", "event": "run_start"},
            {"ts": "2026-10-20T20:25:10Z", "event": "run_error", "error": "boom"},
        ],
    )
    assert run_ended_at(run_dir) == _epoch("2026-10-20T20:25:10Z")


def test_run_ended_at_is_none_without_a_terminal_record_or_with_a_bad_ts(tmp_path: Path) -> None:
    open_run = _write_events(
        tmp_path / "open",
        [
            {"ts": "2026-10-20T20:22:42Z", "event": "run_start"},
            {"ts": "2026-10-20T20:32:42Z", "event": "step_start"},
        ],
    )
    assert run_ended_at(open_run) is None
    bad_ts = _write_events(
        tmp_path / "bad",
        [
            {"ts": "2026-10-20T20:22:42Z", "event": "run_start"},
            {"ts": "not-a-time", "event": "run_end"},
        ],
    )
    assert run_ended_at(bad_ts) is None
    assert run_ended_at(tmp_path / "missing") is None


def test_last_record_at_returns_the_last_parseable_record(tmp_path: Path) -> None:
    run_dir = _write_events(
        tmp_path / "run",
        [
            {"ts": "2026-10-20T20:22:42Z", "event": "run_start"},
            {"ts": "2026-10-20T20:32:42Z", "event": "step_start"},
            {"ts": "garbage", "event": "step_end"},
            {"event": "no_ts"},
        ],
    )
    assert last_record_at(run_dir) == _epoch("2026-10-20T20:32:42Z")
    assert last_record_at(tmp_path / "missing") is None


def test_frontend_pid_comes_from_the_last_hyphen_segment() -> None:
    assert frontend_pid("20261019-120001-4242") == 4242
    assert frontend_pid("nohyphens") is None


def test_frontend_alive_is_false_without_bakar_in_the_cmdline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real_read = observability.Path.read_bytes
    cmdlines = {os.getpid(): b"/usr/bin/sleep\x00600\x00"}

    def fake_read(self: Path) -> bytes:
        parts = self.parts
        if len(parts) == 4 and parts[1] == "proc":
            try:
                return cmdlines[int(parts[2])]
            except KeyError:
                raise FileNotFoundError from None
        return real_read(self)

    monkeypatch.setattr(observability.Path, "read_bytes", fake_read)
    run_dir = tmp_path / f"20261019-120001-{os.getpid()}"
    assert frontend_alive(run_dir) is False
    cmdlines[os.getpid()] = b"/usr/bin/python\x00bakar\x00build\x00"
    assert frontend_alive(run_dir) is True
