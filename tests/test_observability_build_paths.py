"""Tests for the ``build_paths`` run event and its reader."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bakar.commands.status import _event_failure, _terminal_exit_code
from bakar.observability import RunLogger, iter_run_events, last_build_paths


def _write_events(run_dir: Path, *records: dict[str, object]) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"ts": "2026-10-19T12:00:00Z", **rec}) for rec in records]
    (run_dir / "events.jsonl").write_text("\n".join(lines) + "\n")


@pytest.mark.unit
def test_round_trip_through_a_real_run_logger(tmp_path: Path) -> None:
    tmpdir = tmp_path / "build" / "tmp"
    sstate = tmp_path / "sstate-cache"
    with RunLogger(tmp_path / "runs") as log:
        log.build_paths(tmpdir, sstate)
        run_dir = log.run_dir

    recs = [r for r in iter_run_events(run_dir / "events.jsonl") if r.get("event") == "build_paths"]
    assert len(recs) == 1
    assert set(recs[0]) == {"ts", "event", "tmpdir", "sstate_dir"}
    assert recs[0]["tmpdir"] == str(tmpdir)
    assert last_build_paths(run_dir) == (tmpdir, sstate)


@pytest.mark.unit
def test_relative_input_is_recorded_absolute_and_none_as_null(tmp_path: Path) -> None:
    with RunLogger(tmp_path / "runs") as log:
        log.build_paths(Path("build/tmp"), None)
        run_dir = log.run_dir

    rec = next(r for r in iter_run_events(run_dir / "events.jsonl") if r.get("event") == "build_paths")
    assert rec["sstate_dir"] is None
    assert Path(rec["tmpdir"]).is_absolute()
    tmp, sstate = last_build_paths(run_dir)
    assert tmp is not None
    assert tmp.is_absolute()
    assert sstate is None


@pytest.mark.unit
def test_build_paths_after_close_does_not_raise(tmp_path: Path) -> None:
    with RunLogger(tmp_path / "runs") as log:
        pass
    log.build_paths(tmp_path / "tmp", tmp_path / "sstate")


@pytest.mark.unit
def test_missing_events_file_gives_none_pair(tmp_path: Path) -> None:
    assert last_build_paths(tmp_path / "no-such-run") == (None, None)


@pytest.mark.unit
def test_run_without_the_event_gives_none_pair(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    _write_events(run_dir, {"event": "run_start", "run_id": "x"})
    assert last_build_paths(run_dir) == (None, None)


@pytest.mark.unit
@pytest.mark.parametrize("value", ["build/tmp", "", 42, None, ["/abs"], {"p": "/abs"}])
def test_relative_or_non_string_value_reads_as_none(tmp_path: Path, value: object) -> None:
    run_dir = tmp_path / "run"
    _write_events(run_dir, {"event": "build_paths", "tmpdir": value, "sstate_dir": "/srv/sstate"})
    assert last_build_paths(run_dir) == (None, Path("/srv/sstate"))


@pytest.mark.unit
def test_later_of_two_events_wins(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    _write_events(
        run_dir,
        {"event": "build_paths", "tmpdir": "/first/tmp", "sstate_dir": "/first/sstate"},
        {"event": "build_paths", "tmpdir": "/second/tmp", "sstate_dir": None},
    )
    assert last_build_paths(run_dir) == (Path("/second/tmp"), None)


@pytest.mark.unit
def test_event_failure_never_names_build_paths(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    _write_events(
        run_dir,
        {"event": "run_start", "run_id": "x"},
        {"event": "build_paths", "tmpdir": "/b/tmp", "sstate_dir": "/b/sstate"},
        {"event": "step_fail", "step": "kas_build", "reason": "exit_code=1"},
    )
    failure = _event_failure(run_dir)
    assert failure is not None
    assert failure["step"] == "kas_build"
    assert failure["error"] == "exit_code=1"


@pytest.mark.unit
def test_terminal_exit_code_ignores_build_paths(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    _write_events(
        run_dir,
        {"event": "run_start", "run_id": "x"},
        {"event": "build_paths", "tmpdir": "/b/tmp", "sstate_dir": "/b/sstate"},
    )
    assert _terminal_exit_code(run_dir) is None
