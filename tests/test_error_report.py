"""Tests for the write_error_report / analyse round trip.

Covers:
(a) write_error_report produces a valid JSON with all required keys.
(b) analyse fast path reads error-report.json (JSON recipe wins over kas.log recipe).
(c) analyse falls back to live-parse when error-report.json is absent.
(d) write_error_report does not raise on a read-only run_dir (best-effort).
"""

from __future__ import annotations

import json
import stat
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from bakar.triage import analyse, write_error_report

if TYPE_CHECKING:
    from pathlib import Path

# Minimal cfg stub: write_error_report only reads .machine, .distro, .bsp_family.
_CFG = SimpleNamespace(machine="imx8mp-var-dart", distro="fsl-imx-xwayland", bsp_family="nxp")

_REQUIRED_KEYS = {"step", "machine", "distro", "bsp_family", "exit_code", "kas_log_tail", "recipe_errors", "suggestions"}


@pytest.mark.unit
def test_write_error_report_produces_valid_json(tmp_path: Path) -> None:
    """(a) write_error_report creates a JSON file with all required keys."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    kas_log = run_dir / "kas.log"
    kas_log.write_text(
        "NOTE: Executing Tasks\n"
        "ERROR: myrecipe-1.0-r0 do_compile: Function failed: do_compile\n"
        "NOTE: Tasks Summary: 1 task failed.\n"
    )

    write_error_report(run_dir, _CFG, exit_code=1)

    report_path = run_dir / "error-report.json"
    assert report_path.is_file(), "error-report.json was not created"

    data = json.loads(report_path.read_text())
    missing = _REQUIRED_KEYS - data.keys()
    assert not missing, f"missing keys in error-report.json: {missing}"

    # Spot-check populated values.
    assert data["machine"] == "imx8mp-var-dart"
    assert data["distro"] == "fsl-imx-xwayland"
    assert data["bsp_family"] == "nxp"
    assert data["exit_code"] == 1
    assert isinstance(data["kas_log_tail"], list)
    assert isinstance(data["recipe_errors"], list)
    assert isinstance(data["suggestions"], list)

    # Recipe from kas.log should appear in recipe_errors.
    recipes = [e["recipe"] for e in data["recipe_errors"]]
    assert any("myrecipe" in r for r in recipes)


@pytest.mark.unit
def test_analyse_fast_path_uses_json_recipe(tmp_path: Path) -> None:
    """(b) analyse reads error-report.json; the JSON recipe beats the kas.log recipe."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    # kas.log contains a DIFFERENT recipe from the one in the JSON.
    kas_log = run_dir / "kas.log"
    kas_log.write_text(
        "ERROR: test-recipe-log do_compile: Function failed from log\n"
    )

    # Write error-report.json with a distinct recipe.
    report = {
        "step": "kas_build",
        "machine": "imx8mp-var-dart",
        "distro": "fsl-imx-xwayland",
        "bsp_family": "nxp",
        "exit_code": 1,
        "kas_log_tail": [],
        "recipe_errors": [
            {"recipe": "test-recipe-json", "task": "compile", "excerpt": "from json"},
        ],
        "suggestions": [],
    }
    (run_dir / "error-report.json").write_text(json.dumps(report))

    result = analyse(run_dir, workspace=tmp_path)

    json_recipes = [e.recipe for e in result.recipe_errors]
    assert "test-recipe-json" in json_recipes, "JSON recipe not in report"
    assert "test-recipe-log" not in json_recipes, "kas.log recipe leaked into fast-path report"


@pytest.mark.unit
def test_analyse_fallback_when_no_error_report(tmp_path: Path) -> None:
    """(c) analyse falls back to live-parse and still produces a report."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    # No error-report.json - only kas.log.
    kas_log = run_dir / "kas.log"
    kas_log.write_text(
        "NOTE: Executing Tasks\n"
        "ERROR: fallback-recipe-1.0-r0 do_compile: Function failed: do_compile\n"
        "NOTE: Tasks Summary: 1 task failed.\n"
    )

    assert not (run_dir / "error-report.json").exists()

    result = analyse(run_dir, workspace=tmp_path)

    # Must return a valid TriageReport.
    assert result is not None
    assert result.run_dir == run_dir
    # Recipe from kas.log is surfaced via live-parse.
    live_recipes = [e.recipe for e in result.recipe_errors]
    assert any("fallback-recipe" in r for r in live_recipes)


@pytest.mark.unit
def test_write_error_report_silent_on_read_only_dir(tmp_path: Path) -> None:
    """(d) write_error_report does not raise when run_dir is read-only."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    kas_log = run_dir / "kas.log"
    kas_log.write_text("NOTE: nothing here\n")

    # Make the directory read-only so the write will fail with OSError.
    original_mode = run_dir.stat().st_mode
    run_dir.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        # Must not raise.
        write_error_report(run_dir, _CFG, exit_code=2)
    finally:
        run_dir.chmod(original_mode)
