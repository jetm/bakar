"""Tests for :mod:`bakar.insights_timing`.

Covers duration computation from started/completed timestamps, the
missing-``completed`` skip rule, top-N truncation/padding for both more-than-N
and fewer-than-N completed tasks, and the critical-path opt-in section's
graceful degradation when the dependency source is unavailable versus when it
succeeds. The critical-path tests use a scripted stand-in callable for
``dependency_source`` rather than a real ``bitbake -g`` invocation, so the
suite stays hermetic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from bakar.insights_timing import timing_report

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


def _row(recipe: str, task: str, started: float, completed: float | None) -> dict:
    row = {"task": task, "recipe": recipe, "started": started}
    if completed is not None:
        row["completed"] = completed
    return row


@pytest.mark.unit
def test_duration_computed_from_timestamps(tmp_path: Path) -> None:
    artifact = {"tasks": [_row("busybox", "do_compile", 100.0, 142.5)]}

    report = timing_report(artifact, baselines_path=tmp_path / "absent.json")

    assert len(report.top_slowest) == 1
    duration = report.top_slowest[0]
    assert duration.recipe == "busybox"
    assert duration.task == "do_compile"
    assert duration.duration == pytest.approx(42.5)


@pytest.mark.unit
def test_task_missing_completed_is_excluded_without_raising(tmp_path: Path) -> None:
    artifact = {
        "tasks": [
            _row("busybox", "do_compile", 100.0, 142.5),
            _row("zlib", "do_fetch", 200.0, None),
        ]
    }

    report = timing_report(artifact, baselines_path=tmp_path / "absent.json")

    assert [d.recipe for d in report.top_slowest] == ["busybox"]


@pytest.mark.unit
def test_top_n_truncates_when_more_than_n_completed_tasks(tmp_path: Path) -> None:
    artifact = {
        "tasks": [_row(f"recipe{i}", "do_compile", 0.0, float(i)) for i in range(1, 6)]
    }

    report = timing_report(artifact, top_n=3, baselines_path=tmp_path / "absent.json")

    assert len(report.top_slowest) == 3
    # Sorted descending by duration: recipe5 (5s), recipe4 (4s), recipe3 (3s).
    assert [d.recipe for d in report.top_slowest] == ["recipe5", "recipe4", "recipe3"]


@pytest.mark.unit
def test_top_n_unpadded_when_fewer_than_n_completed_tasks(tmp_path: Path) -> None:
    artifact = {
        "tasks": [
            _row("busybox", "do_compile", 0.0, 5.0),
            _row("zlib", "do_compile", 0.0, 3.0),
        ]
    }

    report = timing_report(artifact, top_n=10, baselines_path=tmp_path / "absent.json")

    assert len(report.top_slowest) == 2
    assert [d.recipe for d in report.top_slowest] == ["busybox", "zlib"]


@pytest.mark.unit
def test_critical_path_unavailable_when_dependency_source_raises(tmp_path: Path) -> None:
    artifact = {"tasks": [_row("busybox", "do_compile", 0.0, 42.0)]}

    def _broken_source() -> tuple[str, str]:
        raise RuntimeError("bitbake -g unavailable in this environment")

    report = timing_report(
        artifact,
        baselines_path=tmp_path / "absent.json",
        dependency_source=_broken_source,
    )

    # Duration and top-N sections must stay populated even though the
    # dependency model failed - the falsifier this test guards against is a
    # regression that empties them on critical-path failure.
    assert len(report.top_slowest) == 1
    assert report.top_slowest[0].duration == pytest.approx(42.0)

    assert report.critical_path.available is False
    assert report.critical_path.chain == []
    assert report.critical_path.total_seconds == 0.0
    assert "unavailable" in report.critical_path.note


@pytest.mark.unit
def test_critical_path_unavailable_when_dependency_source_returns_empty_graph(
    tmp_path: Path,
) -> None:
    artifact = {"tasks": [_row("busybox", "do_compile", 0.0, 42.0)]}

    def _empty_source() -> tuple[str, str]:
        return "digraph { }", ""

    report = timing_report(
        artifact,
        baselines_path=tmp_path / "absent.json",
        dependency_source=_empty_source,
    )

    assert len(report.top_slowest) == 1
    assert report.critical_path.available is False
    assert "unavailable" in report.critical_path.note


@pytest.mark.unit
def test_critical_path_available_when_dependency_source_succeeds(tmp_path: Path) -> None:
    artifact = {
        "tasks": [
            _row("a", "do_compile", 0.0, 10.0),
            _row("b", "do_compile", 0.0, 20.0),
        ]
    }

    def _valid_source() -> tuple[str, str]:
        return 'digraph { "a.do_compile" -> "b.do_compile"; }', ""

    report = timing_report(
        artifact,
        baselines_path=tmp_path / "absent.json",
        dependency_source=_valid_source,
    )

    assert len(report.top_slowest) == 2
    assert report.critical_path.available is True
    assert report.critical_path.chain == ["a", "b"]
    assert report.critical_path.total_seconds == pytest.approx(30.0)
