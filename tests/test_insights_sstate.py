"""Tests for :mod:`bakar.insights_sstate`.

Covers the hit/miss counting rules directly against synthetic artifact dicts
(all-hits, mixed hit/miss, multi-recipe sort order, and the no-data signal),
plus one smoke test that the real event-log fixture round-trips through
:func:`bakar.eventlog.normalize` and :func:`bakar.insights_sstate.sstate_report`
without raising.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bakar import eventlog
from bakar.insights_sstate import NO_DATA_MESSAGE, sstate_report

pytestmark = pytest.mark.unit

_FIXTURE = Path(__file__).parent / "fixtures" / "bitbake_eventlog.json"


def _setscene_row(recipe: str, outcome: str) -> dict:
    return {"task": "do_populate_sysroot_setscene", "recipe": recipe, "outcome": outcome}


@pytest.mark.unit
def test_all_covered_tasks_yield_zero_misses() -> None:
    artifact = {
        "tasks": [
            _setscene_row("busybox", "succeeded"),
            _setscene_row("busybox", "succeeded"),
        ]
    }
    report = sstate_report(artifact)

    assert report.message is None
    assert len(report.recipes) == 1
    stat = report.recipes[0]
    assert stat.recipe == "busybox"
    assert stat.hits == 2
    assert stat.misses == 0
    assert stat.miss_ratio == 0.0


@pytest.mark.unit
def test_mixed_hits_and_misses_computes_ratio() -> None:
    artifact = {
        "tasks": [
            _setscene_row("zlib", "succeeded"),
            _setscene_row("zlib", "succeeded"),
            _setscene_row("zlib", "succeeded"),
            _setscene_row("zlib", "failed_silent"),
        ]
    }
    report = sstate_report(artifact)

    assert report.message is None
    assert len(report.recipes) == 1
    stat = report.recipes[0]
    assert stat.recipe == "zlib"
    assert stat.hits == 3
    assert stat.misses == 1
    assert stat.miss_ratio == pytest.approx(0.25)


@pytest.mark.unit
def test_multiple_recipes_sorted_by_descending_misses() -> None:
    artifact = {
        "tasks": [
            *[_setscene_row("linux-imx", "failed_silent") for _ in range(12)],
            *[_setscene_row("zlib", "failed_silent") for _ in range(3)],
            _setscene_row("busybox", "succeeded"),
        ]
    }
    report = sstate_report(artifact)

    assert report.message is None
    assert [stat.recipe for stat in report.recipes] == ["linux-imx", "zlib", "busybox"]
    assert [stat.misses for stat in report.recipes] == [12, 3, 0]


@pytest.mark.unit
def test_no_setscene_rows_yields_no_data_message_not_exception() -> None:
    artifact = {
        "tasks": [
            {"task": "do_compile", "recipe": "busybox", "outcome": "succeeded"},
            {"task": "do_fetch", "recipe": "zlib", "outcome": "failed"},
        ]
    }
    report = sstate_report(artifact)

    assert report.recipes == []
    assert report.message == NO_DATA_MESSAGE


@pytest.mark.unit
def test_real_fixture_round_trips_through_normalize_without_raising() -> None:
    artifact = eventlog.normalize(_FIXTURE)

    report = sstate_report(artifact)

    assert report.message is None or report.message == NO_DATA_MESSAGE
    assert isinstance(report.recipes, list)
