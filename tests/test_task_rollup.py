"""Tests for :mod:`bakar.task_rollup`.

Cover the per-family wall-time summation, the ``other`` fallback bucket, the
skip rules for missing/negative durations, and the best-effort Go-recipe
compile-time subset that never alters the ``do_compile`` family total.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bakar import task_rollup


def _row(recipe: str, task: str, started: float | None, completed: float | None) -> dict:
    row: dict = {"recipe": recipe, "task": task}
    if started is not None:
        row["started"] = started
    if completed is not None:
        row["completed"] = completed
    return row


@pytest.mark.unit
def test_durations_summed_per_family() -> None:
    rows = [
        _row("busybox-1.36-r0", "do_compile", 100.0, 130.0),  # 30s
        _row("zlib-1.3-r0", "do_compile", 200.0, 210.0),  # 10s
        _row("busybox-1.36-r0", "do_configure", 50.0, 55.0),  # 5s
    ]
    result = task_rollup.compute_task_rollup(rows)

    assert result.families["do_compile"].seconds == 40.0
    assert result.families["do_compile"].count == 2
    assert result.families["do_configure"].seconds == 5.0
    assert result.families["do_configure"].count == 1


@pytest.mark.unit
def test_unknown_task_lands_in_other() -> None:
    rows = [_row("busybox-1.36-r0", "do_package_qa", 10.0, 22.0)]
    result = task_rollup.compute_task_rollup(rows)

    assert result.families["other"].seconds == 12.0
    assert result.families["other"].count == 1
    assert result.families["do_compile"].count == 0


@pytest.mark.unit
def test_task_missing_completed_is_excluded_without_raising() -> None:
    rows = [_row("busybox-1.36-r0", "do_compile", 100.0, None)]
    result = task_rollup.compute_task_rollup(rows)

    assert result.families["do_compile"].seconds == 0.0
    assert result.families["do_compile"].count == 0


@pytest.mark.unit
def test_negative_duration_is_excluded() -> None:
    rows = [
        _row("busybox-1.36-r0", "do_compile", 200.0, 100.0),  # -100s, excluded
        _row("zlib-1.3-r0", "do_compile", 100.0, 130.0),  # 30s, kept
    ]
    result = task_rollup.compute_task_rollup(rows)

    assert result.families["do_compile"].seconds == 30.0
    assert result.families["do_compile"].count == 1


@pytest.mark.unit
def test_go_recipe_compile_seconds_subset_does_not_alter_family_total() -> None:
    rows = [
        _row("go-1.22-r0", "do_compile", 100.0, 120.0),  # 20s Go
        _row("zlib-1.3-r0", "do_compile", 200.0, 230.0),  # 30s C
    ]
    result = task_rollup.compute_task_rollup(rows)

    assert result.families["do_compile"].seconds == 50.0
    assert result.families["do_compile"].count == 2
    assert result.go_compile_seconds == 20.0


@pytest.mark.unit
def test_no_go_recipes_yields_zero_go_compile_seconds() -> None:
    rows = [_row("zlib-1.3-r0", "do_compile", 200.0, 230.0)]
    result = task_rollup.compute_task_rollup(rows)

    assert result.go_compile_seconds == 0.0


@pytest.mark.unit
def test_go_signal_is_prefix_or_exact_not_substring() -> None:
    rows = [
        _row("gobject-introspection-1.78-r0", "do_compile", 0.0, 5.0),
        _row("google-croscore-1.0-r0", "do_compile", 0.0, 7.0),
        _row("golang-1.22-r0", "do_compile", 0.0, 11.0),
        _row("golang-github-foo-1.0-r0", "do_compile", 0.0, 3.0),
    ]
    result = task_rollup.compute_task_rollup(rows)

    # gobject-introspection and google-* must NOT match; golang and golang-* must.
    assert result.go_compile_seconds == 14.0


@pytest.mark.unit
def test_all_families_present_when_empty() -> None:
    result = task_rollup.compute_task_rollup([])

    for family in ("do_compile", "do_configure", "do_install", "do_fetch", "other"):
        assert result.families[family].seconds == 0.0
        assert result.families[family].count == 0
    assert result.go_compile_seconds == 0.0


@pytest.mark.unit
def test_reads_events_artifact_path(tmp_path: Path) -> None:
    artifact = tmp_path / "bitbake-events.json"
    artifact.write_text(
        json.dumps({"tasks": [_row("zlib-1.3-r0", "do_fetch", 10.0, 25.0)]}),
        encoding="utf-8",
    )
    result = task_rollup.compute_task_rollup(artifact)

    assert result.families["do_fetch"].seconds == 15.0
    assert result.families["do_fetch"].count == 1


@pytest.mark.unit
def test_missing_artifact_returns_zeroed_rollup(tmp_path: Path) -> None:
    result = task_rollup.compute_task_rollup(tmp_path / "absent.json")

    assert result.families["do_compile"].count == 0
    assert result.go_compile_seconds == 0.0


@pytest.mark.unit
def test_malformed_artifact_returns_zeroed_rollup(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    result = task_rollup.compute_task_rollup(bad)

    assert result.families["do_compile"].count == 0
    assert result.go_compile_seconds == 0.0
