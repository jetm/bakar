"""Tests for :mod:`bakar.task_timings`.

Cover the Welford accumulator, the JSON read/write round-trip with the
exact file schema, the duration-extraction skip rules against the
``bitbake-events.json`` artifact shape, and the all-errors-return-empty
contract of :func:`load_baselines`.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from bakar import task_timings


@pytest.mark.unit
def test_default_path_is_under_state() -> None:
    assert Path.home() / ".local/state/bakar/task-timings.json" == task_timings.DEFAULT_TIMINGS_PATH


@pytest.mark.unit
def test_load_baselines_missing_file_returns_empty(tmp_path: Path) -> None:
    assert task_timings.load_baselines(tmp_path / "absent.json") == {}


@pytest.mark.unit
def test_load_baselines_malformed_json_returns_empty(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert task_timings.load_baselines(bad) == {}


@pytest.mark.unit
def test_load_baselines_missing_keys_returns_empty(tmp_path: Path) -> None:
    f = tmp_path / "t.json"
    f.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
    assert task_timings.load_baselines(f) == {}


@pytest.mark.unit
def test_load_baselines_single_sample_zero_stddev(tmp_path: Path) -> None:
    f = tmp_path / "t.json"
    f.write_text(
        json.dumps({"schema_version": 2, "tasks": {"glibc:do_compile": {"count": 1, "mean": 10.0, "m2": 0.0}}}),
        encoding="utf-8",
    )
    baselines = task_timings.load_baselines(f)
    assert baselines == {"glibc:do_compile": (10.0, 0.0)}


@pytest.mark.unit
def test_load_baselines_sample_stddev(tmp_path: Path) -> None:
    # Two samples 10 and 20: mean 15, m2 = 50, sample var = 50/1 = 50.
    f = tmp_path / "t.json"
    f.write_text(
        json.dumps({"schema_version": 2, "tasks": {"glibc:do_compile": {"count": 2, "mean": 15.0, "m2": 50.0}}}),
        encoding="utf-8",
    )
    mean, stddev = task_timings.load_baselines(f)["glibc:do_compile"]
    assert mean == 15.0
    assert stddev == pytest.approx(math.sqrt(50.0))


@pytest.mark.unit
def test_welford_matches_batch_statistics() -> None:
    samples = [4.0, 8.0, 15.0, 16.0, 23.0, 42.0]
    entry: dict = {}
    for x in samples:
        task_timings._welford_update(entry, x)

    n = len(samples)
    expected_mean = sum(samples) / n
    expected_m2 = sum((x - expected_mean) ** 2 for x in samples)

    assert entry["count"] == n
    assert entry["mean"] == pytest.approx(expected_mean)
    assert entry["m2"] == pytest.approx(expected_m2)
    assert entry["min"] == min(samples)
    assert entry["max"] == max(samples)


@pytest.mark.unit
def test_welford_first_sample_defaults() -> None:
    entry: dict = {}
    task_timings._welford_update(entry, 7.0)
    assert entry == {"count": 1, "mean": 7.0, "m2": 0.0, "min": 7.0, "max": 7.0}


def _write_events(path: Path, tasks: list[dict]) -> None:
    path.write_text(json.dumps({"schema_version": 1, "tasks": tasks}), encoding="utf-8")


@pytest.mark.unit
def test_update_from_events_creates_file_and_parent(tmp_path: Path) -> None:
    events = tmp_path / "bitbake-events.json"
    _write_events(events, [{"recipe": "glibc-2.39-r0", "task": "do_compile", "started": 100.0, "completed": 130.0}])
    timings = tmp_path / "nested" / "task-timings.json"

    task_timings.update_from_events(events, timings)

    assert timings.is_file()
    data = json.loads(timings.read_text(encoding="utf-8"))
    assert data["schema_version"] == 2
    entry = data["tasks"]["glibc:do_compile"]
    assert entry["count"] == 1
    assert entry["mean"] == pytest.approx(30.0)
    assert entry["min"] == pytest.approx(30.0)
    assert entry["max"] == pytest.approx(30.0)


@pytest.mark.unit
def test_update_from_events_skips_missing_and_negative(tmp_path: Path) -> None:
    events = tmp_path / "events.json"
    _write_events(
        events,
        [
            {"recipe": "glibc-2.39-r0", "task": "do_compile", "started": 100.0, "completed": 130.0},
            {"recipe": "a-1.0-r0", "task": "do_install", "started": None, "completed": 5.0},
            {"recipe": "b-1.0-r0", "task": "do_fetch", "started": 5.0, "completed": None},
            {"recipe": "c-1.0-r0", "task": "do_clock_skew", "started": 200.0, "completed": 190.0},
            {"recipe": "x", "started": 1.0, "completed": 2.0},
        ],
    )
    timings = tmp_path / "t.json"
    task_timings.update_from_events(events, timings)

    data = json.loads(timings.read_text(encoding="utf-8"))
    assert set(data["tasks"]) == {"glibc:do_compile"}


@pytest.mark.unit
def test_update_from_events_accumulates_across_calls(tmp_path: Path) -> None:
    timings = tmp_path / "t.json"
    for completed in (110.0, 130.0):
        events = tmp_path / "events.json"
        _write_events(
            events, [{"recipe": "glibc-2.39-r0", "task": "do_compile", "started": 100.0, "completed": completed}]
        )
        task_timings.update_from_events(events, timings)

    baselines = task_timings.load_baselines(timings)
    mean, stddev = baselines["glibc:do_compile"]
    # Durations 10 and 30: mean 20, sample stddev sqrt(200).
    assert mean == pytest.approx(20.0)
    assert stddev == pytest.approx(math.sqrt(200.0))


@pytest.mark.unit
def test_update_from_events_tolerates_malformed_existing(tmp_path: Path) -> None:
    timings = tmp_path / "t.json"
    timings.write_text("garbage{", encoding="utf-8")
    events = tmp_path / "events.json"
    _write_events(events, [{"recipe": "glibc-2.39-r0", "task": "do_compile", "started": 100.0, "completed": 110.0}])

    task_timings.update_from_events(events, timings)

    data = json.loads(timings.read_text(encoding="utf-8"))
    assert data["tasks"]["glibc:do_compile"]["count"] == 1


@pytest.mark.unit
def test_update_from_events_missing_artifact_is_noop(tmp_path: Path) -> None:
    timings = tmp_path / "t.json"
    task_timings.update_from_events(tmp_path / "absent.json", timings)
    assert not timings.exists()


@pytest.mark.unit
def test_baseline_key_strips_pf_version() -> None:
    assert task_timings.baseline_key("glibc-2.39-r0", "do_compile") == "glibc:do_compile"
    assert task_timings.baseline_key("busybox-1.36.1-r0", "do_fetch") == "busybox:do_fetch"
    assert task_timings.baseline_key("gcc-cross-x86_64-13.2-r0", "do_compile") == "gcc-cross-x86_64:do_compile"
    # No version suffix: PF used verbatim. Empty recipe keeps the separator.
    assert task_timings.baseline_key("weird-pf", "do_x") == "weird-pf:do_x"
    assert task_timings.baseline_key("", "do_x") == ":do_x"


@pytest.mark.unit
def test_load_baselines_discards_old_schema_version(tmp_path: Path) -> None:
    f = tmp_path / "t.json"
    f.write_text(
        json.dumps({"schema_version": 1, "tasks": {"do_compile": {"count": 5, "mean": 9.0, "m2": 0.0}}}),
        encoding="utf-8",
    )
    assert task_timings.load_baselines(f) == {}


@pytest.mark.unit
def test_update_from_events_discards_old_schema_version(tmp_path: Path) -> None:
    timings = tmp_path / "t.json"
    timings.write_text(
        json.dumps({"schema_version": 1, "tasks": {"do_compile": {"count": 5, "mean": 9.0, "m2": 0.0}}}),
        encoding="utf-8",
    )
    events = tmp_path / "events.json"
    _write_events(events, [{"recipe": "glibc-2.39-r0", "task": "do_compile", "started": 100.0, "completed": 110.0}])

    task_timings.update_from_events(events, timings)

    data = json.loads(timings.read_text(encoding="utf-8"))
    # v1 bare-task data discarded wholesale; only the v2 recipe-keyed entry remains.
    assert data["schema_version"] == 2
    assert set(data["tasks"]) == {"glibc:do_compile"}
    assert data["tasks"]["glibc:do_compile"]["count"] == 1
