"""Hermetic unit tests for :mod:`bakar.triage`.

Targets the parse/scan helpers and the :func:`analyse` entrypoint. All
tests run against synthetic files under ``tmp_path`` (via the shared
``fake_run_dir`` fixture) or files built inline; no real run directory
is touched.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from bakar.triage import (
    _last_event_matching,
    _match_suggestions,
    _scan_recipe_errors,
    _tail,
    analyse,
    find_runs,
)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.unit
def test_last_event_matching_returns_step_fail(fake_run_dir: Path) -> None:
    events = fake_run_dir / "events.jsonl"

    rec = _last_event_matching(events, "step_fail")

    assert rec is not None
    assert rec["event"] == "step_fail"
    assert rec["step"] == "kas-build"


@pytest.mark.unit
def test_last_event_matching_returns_none_for_unknown_event(fake_run_dir: Path) -> None:
    events = fake_run_dir / "events.jsonl"

    assert _last_event_matching(events, "step_does_not_exist") is None


@pytest.mark.unit
def test_tail_returns_last_n_lines(tmp_path: Path) -> None:
    log = tmp_path / "multi.log"
    log.write_text("one\ntwo\nthree\nfour\nfive\n")

    assert _tail(log, n=2) == ["four", "five"]


@pytest.mark.unit
def test_tail_returns_all_lines_when_n_exceeds_file(tmp_path: Path) -> None:
    log = tmp_path / "short.log"
    log.write_text("alpha\nbeta\ngamma\n")

    result = _tail(log, n=100)

    assert result == ["alpha", "beta", "gamma"]


@pytest.mark.unit
def test_scan_recipe_errors_finds_linux_imx(fake_run_dir: Path) -> None:
    kas_log = fake_run_dir / "kas.log"

    errors = _scan_recipe_errors(kas_log, cap=10)

    assert len(errors) >= 1
    assert any("linux-imx" in e.recipe for e in errors)
    assert all(
        e.task in {"fetch", "compile", "configure", "install", "populate_sysroot", "rootfs", "unpack", "patch"}
        for e in errors
    )


@pytest.mark.unit
def test_scan_recipe_errors_honors_cap(tmp_path: Path) -> None:
    # Build a kas.log with two distinct recipe errors so the cap can
    # demonstrably truncate the result.  Note: triage._scan_recipe_errors
    # checks ``len(out) >= cap`` *after* the append, so cap=0 still yields
    # one entry; cap=1 is the smallest value that exercises a real cap.
    log = tmp_path / "kas.log"
    log.write_text(
        "ERROR: recipe-a-1.0-r0 do_compile: Function failed: do_compile\n"
        "ERROR: recipe-b-2.0-r0 do_fetch: Fetcher failure: foo\n"
    )

    capped = _scan_recipe_errors(log, cap=1)

    assert len(capped) == 1
    assert capped[0].recipe == "recipe-a-1.0-r0"


@pytest.mark.unit
def test_match_suggestions_returns_list_on_plain_text() -> None:
    result = _match_suggestions("nothing interesting here\njust prose\n")

    assert isinstance(result, list)


@pytest.mark.unit
def test_match_suggestions_does_not_raise_on_fork_race_shape() -> None:
    # Shape adapted from the fork-race signatures; exact content does not
    # matter - the test asserts the helper tolerates the input and returns
    # a list (possibly with one or more hits) without raising.
    fork_race_text = (
        "ERROR: Unable to start bitbake server (None)\n"
        "BBHandledException\n"
        "PermissionError: [Errno 13] Permission denied\n"
    )

    result = _match_suggestions(fork_race_text)

    assert isinstance(result, list)


@pytest.mark.unit
def test_analyse_returns_report_with_failed_step_and_recipe_errors(fake_run_dir: Path, tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    report = analyse(fake_run_dir, workspace)

    assert report.failing_step == "kas-build"
    assert report.recipe_errors, "expected at least one recipe-level failure"
    assert any("linux-imx" in e.recipe for e in report.recipe_errors)


@pytest.mark.unit
def test_find_runs_returns_newest_first(tmp_path: Path) -> None:
    runs_root = tmp_path / "nxp" / "build" / "runs"
    runs_root.mkdir(parents=True)
    (runs_root / "20260101-000000").mkdir()
    (runs_root / "20260201-000000").mkdir()
    newest = runs_root / "20260301-000000"
    newest.mkdir()

    runs = find_runs(tmp_path)

    assert len(runs) == 3
    assert runs[0] == newest
    assert [r.name for r in runs] == [
        "20260301-000000",
        "20260201-000000",
        "20260101-000000",
    ]
