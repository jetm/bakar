"""Tests for the stop-request record and ``step_fail_text``."""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

import pytest

from bakar import observability
from bakar.observability import (
    STOP_REQUEST_FILENAME,
    RunLogger,
    iter_run_events,
    read_stop_request,
    step_fail_text,
    write_stop_request,
)

if TYPE_CHECKING:
    from pathlib import Path


def _tmp_files(run_dir: Path) -> list[Path]:
    return list(run_dir.glob(".stop-request.*.tmp"))


@pytest.mark.unit
@pytest.mark.parametrize("force", [False, True])
def test_round_trip(tmp_path: Path, force: bool) -> None:
    assert write_stop_request(tmp_path, force=force) is True
    rec = read_stop_request(tmp_path)
    assert rec is not None
    assert rec["by"] == "bakar stop"
    assert rec["force"] is force
    assert isinstance(rec["requested_at"], str)
    assert _tmp_files(tmp_path) == []


@pytest.mark.unit
def test_second_write_replaces_first(tmp_path: Path) -> None:
    assert write_stop_request(tmp_path)
    assert write_stop_request(tmp_path, force=True)
    rec = read_stop_request(tmp_path)
    assert rec is not None
    assert rec["force"] is True
    json.loads((tmp_path / STOP_REQUEST_FILENAME).read_text())


@pytest.mark.unit
def test_missing_directory_returns_false(tmp_path: Path) -> None:
    assert write_stop_request(tmp_path / "nope") is False


@pytest.mark.unit
@pytest.mark.parametrize("content", [None, "", "not json at all", "[1, 2]", '"str"'])
def test_read_returns_none_for_unusable_file(tmp_path: Path, content: str | None) -> None:
    if content is not None:
        (tmp_path / STOP_REQUEST_FILENAME).write_text(content)
    assert read_stop_request(tmp_path) is None


@pytest.mark.unit
def test_replace_failure_leaves_no_temp_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(src: object, dst: object) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", boom)
    assert write_stop_request(tmp_path) is False
    assert _tmp_files(tmp_path) == []
    assert not (tmp_path / STOP_REQUEST_FILENAME).exists()


@pytest.mark.unit
def test_writer_leaves_run_logger_events_intact(tmp_path: Path) -> None:
    with RunLogger(tmp_path / "runs") as log:
        log.info("before")
        assert write_stop_request(log.run_dir)
        log.info("after")
        run_dir = log.run_dir
    msgs = [r.get("message") for r in iter_run_events(run_dir / "events.jsonl")]
    assert "before" in msgs
    assert "after" in msgs
    assert read_stop_request(run_dir) is not None


@pytest.mark.unit
@pytest.mark.parametrize(
    ("rec", "expected"),
    [
        ({"error": "boom"}, "boom"),
        ({"reason": "exit_code=2"}, "exit_code=2"),
        ({"error": "boom", "reason": "exit_code=2"}, "boom"),
        ({"error": "", "reason": "exit_code=2"}, "exit_code=2"),
        ({"error": 5, "reason": ["x"]}, None),
        ({}, None),
    ],
)
def test_step_fail_text(rec: dict, expected: str | None) -> None:
    assert step_fail_text(rec) == expected


@pytest.mark.unit
def test_constants() -> None:
    assert observability.STOPPED_STEP == "stopped"
    assert observability.STOPPED_ERROR == "stopped by bakar stop"
