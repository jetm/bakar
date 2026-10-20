"""Tests for the ``build_config`` run event and its reader."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from bakar.observability import RunLogger, iter_run_events, last_build_config

if TYPE_CHECKING:
    from pathlib import Path


def _write_events(run_dir: Path, *records: dict[str, object]) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"ts": "2026-10-19T12:00:00Z", **rec}) for rec in records]
    (run_dir / "events.jsonl").write_text("\n".join(lines) + "\n")


@pytest.mark.unit
def test_round_trip_through_a_real_run_logger(tmp_path: Path) -> None:
    work = tmp_path / "bsp"
    with RunLogger(tmp_path / "runs") as log:
        log.build_config("nxp", "imx93-var-som", "container", work)
        run_dir = log.run_dir

    recs = [r for r in iter_run_events(run_dir / "events.jsonl") if r.get("event") == "build_config"]
    assert len(recs) == 1
    assert set(recs[0]) == {"ts", "event", "family", "machine", "mode", "work_mount"}
    assert recs[0]["work_mount"] == str(work)
    assert last_build_config(run_dir) == {
        "family": "nxp",
        "machine": "imx93-var-som",
        "mode": "container",
        "work_mount": work,
    }


@pytest.mark.unit
@pytest.mark.parametrize("machine", ["generic", ""])
def test_generic_or_empty_machine_reads_back_unknown(tmp_path: Path, machine: str) -> None:
    with RunLogger(tmp_path / "runs") as log:
        log.build_config("byo", machine, "host", None)
        run_dir = log.run_dir

    cfg = last_build_config(run_dir)
    assert cfg is not None
    assert cfg["machine"] == "unknown"
    assert cfg["work_mount"] is None


@pytest.mark.unit
@pytest.mark.parametrize("value", ["bsp/work", "", 42, None, ["/abs"]])
def test_relative_or_non_string_work_mount_reads_as_none(tmp_path: Path, value: object) -> None:
    run_dir = tmp_path / "run"
    _write_events(
        run_dir,
        {"event": "build_config", "family": "ti", "machine": "am62", "mode": "host", "work_mount": value},
    )
    cfg = last_build_config(run_dir)
    assert cfg is not None
    assert cfg["work_mount"] is None
    assert cfg["family"] == "ti"


@pytest.mark.unit
def test_run_without_the_record_reads_none(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    _write_events(run_dir, {"event": "run_start", "run_id": "x"})
    assert last_build_config(run_dir) is None
    assert last_build_config(tmp_path / "no-such-run") is None


@pytest.mark.unit
def test_build_config_after_close_does_not_raise(tmp_path: Path) -> None:
    with RunLogger(tmp_path / "runs") as log:
        pass
    log.build_config("nxp", "m", "host", tmp_path)


@pytest.mark.unit
def test_run_start_still_carries_only_run_id(tmp_path: Path) -> None:
    with RunLogger(tmp_path / "runs") as log:
        log.build_config("nxp", "m", "host", None)
        run_dir = log.run_dir

    start = next(r for r in iter_run_events(run_dir / "events.jsonl") if r.get("event") == "run_start")
    assert set(start) == {"ts", "event", "run_id"}
