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
    write_error_report,
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



@pytest.mark.unit
def test_find_runs_discovers_workspace_root_build(tmp_path: Path) -> None:
    """BYO/bbsetup runs at workspace-root build/runs/ are returned."""
    runs_root = tmp_path / "build" / "runs"
    runs_root.mkdir(parents=True)
    run = runs_root / "20260501-120000"
    run.mkdir()

    runs = find_runs(tmp_path)

    assert run in runs


@pytest.mark.unit
def test_find_runs_discovers_generic_subdir(tmp_path: Path) -> None:
    """Runs under an arbitrary one-level subdir (e.g. byo/) are returned."""
    runs_root = tmp_path / "byo" / "build" / "runs"
    runs_root.mkdir(parents=True)
    run = runs_root / "20260601-090000"
    run.mkdir()

    runs = find_runs(tmp_path)

    assert run in runs


@pytest.mark.unit
def test_find_runs_deduplicates_overlapping_paths(tmp_path: Path) -> None:
    """nxp/ runs are not duplicated even though the glob would also match them."""
    runs_root = tmp_path / "nxp" / "build" / "runs"
    runs_root.mkdir(parents=True)
    (runs_root / "20260101-000000").mkdir()
    (runs_root / "20260201-000000").mkdir()

    runs = find_runs(tmp_path)

    # No duplicates: resolved paths must all be unique.
    resolved = [p.resolve() for p in runs]
    assert len(resolved) == len(set(resolved))
    assert len(runs) == 2


@pytest.mark.unit
def test_find_runs_mixed_families_ordered_newest_first(tmp_path: Path) -> None:
    """Runs from nxp/, ti/, build/, and a BYO subdir are merged and sorted."""
    (tmp_path / "nxp" / "build" / "runs" / "20260101-000000").mkdir(parents=True)
    (tmp_path / "ti" / "build" / "runs" / "20260301-000000").mkdir(parents=True)
    (tmp_path / "build" / "runs" / "20260401-000000").mkdir(parents=True)
    (tmp_path / "byo" / "build" / "runs" / "20260201-000000").mkdir(parents=True)

    runs = find_runs(tmp_path)

    names = [p.name for p in runs]
    assert names == sorted(names, reverse=True)
    assert len(runs) == 4


# ---------------------------------------------------------------------------
# Fast-path tests: analyse reads error-report.json when present
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_analyse_fast_path_uses_json_not_kas_log(tmp_path: Path) -> None:
    """Fast path sources recipe errors from the JSON file, not from kas.log.

    The JSON carries a recipe that does NOT appear in kas.log, and kas.log
    carries a recipe that does NOT appear in the JSON. The report must contain
    the JSON recipe only, proving the fast path was taken.
    """
    run = tmp_path / "run"
    run.mkdir()

    # kas.log has 'kas-log-only-recipe'; JSON has 'json-only-recipe'.
    (run / "kas.log").write_text(
        "ERROR: kas-log-only-recipe-1.0-r0 do_compile: Function failed\n"
    )
    (run / "events.jsonl").write_text(
        '{"event": "step_fail", "step": "kas_build", "ts": "2026-06-01T10:00:00Z"}\n'
    )
    error_report = {
        "step": "kas_build",
        "machine": "imx8mm-var-som",
        "distro": "fslc-framebuffer",
        "bsp_family": "nxp",
        "exit_code": 1,
        "kas_log_tail": ["some tail line"],
        "recipe_errors": [{"recipe": "json-only-recipe-2.0-r0", "task": "fetch", "excerpt": "Fetch failure"}],
        "suggestions": ["Fetch failure: retry, or add a PREMIRROR for the recipe's upstream URL."],
    }
    import json

    (run / "error-report.json").write_text(json.dumps(error_report))

    report = analyse(run, tmp_path)

    assert len(report.recipe_errors) == 1
    assert report.recipe_errors[0].recipe == "json-only-recipe-2.0-r0"
    # kas-log-only-recipe must NOT appear (it came from kas.log, not the JSON)
    assert not any("kas-log-only-recipe" in e.recipe for e in report.recipe_errors)


@pytest.mark.unit
def test_analyse_fast_path_returns_correct_fields(tmp_path: Path) -> None:
    """Fast-path TriageReport carries all expected field values from the JSON."""
    run = tmp_path / "run"
    run.mkdir()
    (run / "kas.log").write_text("")
    (run / "events.jsonl").write_text("")
    error_report = {
        "step": "kas_build",
        "machine": "imx8mm",
        "distro": "fslc-framebuffer",
        "bsp_family": "nxp",
        "exit_code": 2,
        "kas_log_tail": ["line1", "line2"],
        "recipe_errors": [{"recipe": "gstreamer1.0-1.0-r0", "task": "configure", "excerpt": "cmake error"}],
        "suggestions": ["custom suggestion"],
    }
    import json

    (run / "error-report.json").write_text(json.dumps(error_report))

    report = analyse(run, tmp_path)

    assert report.failing_step == "kas_build"
    assert report.kas_log_tail == ["line1", "line2"]
    assert report.recipe_errors[0].task == "configure"
    assert report.recipe_errors[0].excerpt == "cmake error"
    # recipe-level header is prepended to suggestions
    assert any("recipe-level failures" in s for s in report.suggestions)
    assert any("gstreamer1.0-1.0-r0 do_configure:" in s for s in report.suggestions)
    assert "custom suggestion" in report.suggestions


@pytest.mark.unit
def test_analyse_falls_back_when_json_absent(fake_run_dir: Path, tmp_path: Path) -> None:
    """Live-parse path is used when error-report.json does not exist."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    assert not (fake_run_dir / "error-report.json").exists()

    report = analyse(fake_run_dir, workspace)

    assert report.failing_step == "kas-build"
    assert any("linux-imx" in e.recipe for e in report.recipe_errors)


@pytest.mark.unit
def test_analyse_falls_back_on_corrupt_json(tmp_path: Path) -> None:
    """Corrupted error-report.json falls through to the live-parse path."""
    run = tmp_path / "run"
    run.mkdir()
    (run / "events.jsonl").write_text(
        '{"event": "step_fail", "step": "kas_build", "ts": "2026-06-01T10:00:00Z"}\n'
    )
    (run / "kas.log").write_text(
        "ERROR: fallback-recipe-1.0-r0 do_compile: some error\n"
    )
    # Write invalid JSON so the fast path must fall through.
    (run / "error-report.json").write_text("{not valid json}")

    report = analyse(run, tmp_path)

    # Must have fallen back to live parse and found the kas.log recipe.
    assert any("fallback-recipe" in e.recipe for e in report.recipe_errors)


@pytest.mark.unit
def test_analyse_falls_back_on_missing_json_key(tmp_path: Path) -> None:
    """JSON missing a required key falls through to the live-parse path."""
    import json

    run = tmp_path / "run"
    run.mkdir()
    (run / "events.jsonl").write_text(
        '{"event": "step_fail", "step": "kas_build", "ts": "2026-06-01T10:00:00Z"}\n'
    )
    (run / "kas.log").write_text(
        "ERROR: fallback-recipe-2.0-r0 do_fetch: Fetcher failure: bad url\n"
    )
    # Missing 'recipe_errors' key - must fall through.
    (run / "error-report.json").write_text(json.dumps({"step": "kas_build"}))

    report = analyse(run, tmp_path)

    assert any("fallback-recipe-2" in e.recipe for e in report.recipe_errors)
