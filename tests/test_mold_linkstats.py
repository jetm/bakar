"""Tests for :mod:`bakar.mold_linkstats`."""

from __future__ import annotations

import pytest

from bakar.mold_linkstats import (
    aggregate_linklog,
    compare_relink,
    parse_linklog,
)

pytestmark = pytest.mark.unit


# A small fixture log in the exact shared schema (tasks 1.3 / 4.2 / 7.1): one
# JSON object per line, mixing mold and bfd linker records, with the covariates
# the wrapper records. wall_ms values chosen so Σ is easy to eyeball: 1200.
FIXTURE_LINES = [
    '{"linker":"ld.mold","recipe":"librsvg","output":"librsvg-2.so","wall_ms":300,"nproc":16,"loadavg":2.5,"threads":16}',
    '{"linker":"ld.mold","recipe":"busybox","output":"busybox","wall_ms":150,"nproc":16,"loadavg":2.6,"threads":16}',
    '{"linker":"ld.bfd","recipe":"librsvg","output":"librsvg-2.so","wall_ms":500,"nproc":16,"loadavg":3.1,"threads":null}',
    '{"linker":"ld.bfd","recipe":"busybox","output":"busybox","wall_ms":250,"nproc":16,"loadavg":null,"threads":null}',
]

EXPECTED_SUM = 300 + 150 + 500 + 250  # 1200
EXPECTED_COUNT = 4


def test_sigma_equals_sum_of_wall_ms():
    report = aggregate_linklog(FIXTURE_LINES)
    assert report.total_wall_ms == EXPECTED_SUM
    assert report.total_wall_ms == sum(r.wall_ms for r in report.records)


def test_count_equals_number_of_records():
    report = aggregate_linklog(FIXTURE_LINES)
    assert report.count == EXPECTED_COUNT
    assert report.count == len(report.records)


def test_per_linker_breakdown():
    report = aggregate_linklog(FIXTURE_LINES)
    assert set(report.per_linker) == {"ld.mold", "ld.bfd"}
    assert report.per_linker["ld.mold"].total_wall_ms == 450
    assert report.per_linker["ld.mold"].count == 2
    assert report.per_linker["ld.bfd"].total_wall_ms == 750
    assert report.per_linker["ld.bfd"].count == 2


def test_covariates_retained_in_report():
    report = aggregate_linklog(FIXTURE_LINES)
    first = report.records[0]
    # nproc/loadavg/threads must survive into the report, not be dropped.
    assert first.nproc == 16
    assert first.loadavg == 2.5
    assert first.threads == 16
    # A JSON null covariate parses to None rather than being lost.
    bfd = next(r for r in report.records if r.linker == "ld.bfd" and r.recipe == "busybox")
    assert bfd.threads is None
    assert bfd.loadavg is None
    assert bfd.nproc == 16


def test_bad_and_short_lines_handled_gracefully():
    lines = [
        FIXTURE_LINES[0],
        "",  # blank line
        "   ",  # whitespace only
        "not json at all",  # unparseable
        '{"linker":"ld.mold"',  # truncated / short line (partial write)
        '{"recipe":"x","wall_ms":99}',  # missing linker
        '{"linker":"ld.bfd","recipe":"x","output":"x","wall_ms":"nope"}',  # non-numeric wall_ms
        FIXTURE_LINES[2],
    ]
    records = parse_linklog(lines)
    assert len(records) == 2
    report = aggregate_linklog(lines)
    assert report.count == 2
    assert report.total_wall_ms == 300 + 500


def test_aggregate_from_file(tmp_path):
    log = tmp_path / "linklog.jsonl"
    log.write_text("\n".join(FIXTURE_LINES) + "\n", encoding="utf-8")
    report = aggregate_linklog(log)
    assert report.count == EXPECTED_COUNT
    assert report.total_wall_ms == EXPECTED_SUM


def test_missing_file_yields_empty_report(tmp_path):
    report = aggregate_linklog(tmp_path / "does-not-exist.jsonl")
    assert report.count == 0
    assert report.total_wall_ms == 0
    assert report.per_linker == {}


def test_compare_relink_headline():
    mold_arm = [r for r in FIXTURE_LINES if '"ld.mold"' in r]
    baseline_arm = [r for r in FIXTURE_LINES if '"ld.bfd"' in r]
    cmp = compare_relink(mold_source=mold_arm, baseline_source=baseline_arm)
    assert cmp.mold.total_wall_ms == 450
    assert cmp.baseline.total_wall_ms == 750
    assert cmp.delta_ms == 300
    assert cmp.speedup == pytest.approx(750 / 450)


def test_compare_relink_zero_mold_no_op():
    cmp = compare_relink(mold_source=[], baseline_source=FIXTURE_LINES)
    # A relink that executed no real link -> speedup is None (no-op headline).
    assert cmp.mold.total_wall_ms == 0
    assert cmp.speedup is None
