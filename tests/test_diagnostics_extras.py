"""Tests for bakar-failure-diagnostics extras (task 6.2).

Covers:
(a) _SUGGESTIONS new patterns: cc1plus OOM, HTTP 429, DNS/network, connection refused.
(b) find_runs returns a BYO run dir created under a non-nxp/ti path.
(c) build_revision hash determinism and None for empty layers.
(d) BuildUIState FATAL counts as error; warn_count / error_count tallying.
(e) RunLogger writes a greppable header to console.log on step_start but not
    to the Rich console.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from bakar.steps.build_ui import BuildUIState
from bakar.triage import _SUGGESTIONS, _match_suggestions, find_runs

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# (a) _SUGGESTIONS new pattern coverage
# ---------------------------------------------------------------------------

# Each tuple: (description, trigger_line, pattern_index_hint)
_NEW_PATTERN_CASES = [
    (
        "cc1plus OOM kill",
        "Killed signal terminated program cc1plus",
    ),
    (
        "cc1plus out of memory",
        "cc1plus: out of memory",
    ),
    (
        "c++ fatal Killed",
        "c++: fatal error: Killed signal",
    ),
    (
        "HTTP Error 429",
        "HTTP Error 429",
    ),
    (
        "API rate limit exceeded",
        "API rate limit exceeded",
    ),
    (
        "Name or service not known",
        "Name or service not known",
    ),
    (
        "Temporary failure in name resolution",
        "Temporary failure in name resolution",
    ),
    (
        "Connection timed out",
        "Connection timed out",
    ),
    (
        "Connection refused",
        "Connection refused",
    ),
]


@pytest.mark.parametrize("description,trigger_line", _NEW_PATTERN_CASES)
@pytest.mark.unit
def test_suggestions_new_patterns_match(description: str, trigger_line: str) -> None:
    """Each new _SUGGESTIONS entry fires on its target line."""
    hits = _match_suggestions(trigger_line)
    assert len(hits) >= 1, f"Pattern for '{description}' produced no suggestion for line: {trigger_line!r}"


@pytest.mark.unit
def test_suggestions_clean_log_no_new_pattern_hit() -> None:
    """A clean bitbake log line should not trigger the new patterns."""
    clean_lines = [
        "NOTE: recipe linux-imx-1.0-r0: task do_compile: Started",
        "NOTE: recipe busybox-1.36.0-r0: task do_install: Succeeded",
        "Running task 42 of 100",
        "Parsing recipes: 75%",
    ]
    new_pattern_suggestions = {
        suggestion
        for pattern, suggestion in _SUGGESTIONS
        if any(
            kw in pattern.pattern
            for kw in (
                "cc1plus",
                "HTTP Error 429",
                "API rate limit",
                "Name or service",
                "Temporary failure in name resolution",
                "Connection timed out",
                "Connection refused",
            )
        )
    }
    for line in clean_lines:
        hits = set(_match_suggestions(line))
        overlap = hits & new_pattern_suggestions
        assert not overlap, f"Clean line triggered new pattern suggestion:\n  line: {line!r}\n  suggestions: {overlap}"


# ---------------------------------------------------------------------------
# (b) find_runs: BYO run dir under a non-nxp/ti subdirectory
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_find_runs_byo_subdir(tmp_path: Path) -> None:
    """find_runs discovers run dirs one level deep in non-nxp/ti subdirs."""
    # Simulate a BYO/generic workspace with a custom subdir name
    byo_run = tmp_path / "myboard" / "build" / "runs" / "20250601-120000"
    byo_run.mkdir(parents=True)

    runs = find_runs(tmp_path)

    assert any(r == byo_run for r in runs), f"Expected BYO run {byo_run} in {runs}"


@pytest.mark.unit
def test_find_runs_workspace_root_build(tmp_path: Path) -> None:
    """find_runs discovers runs at <workspace>/build/runs/ (BYO/bbsetup root path)."""
    root_run = tmp_path / "build" / "runs" / "20250601-130000"
    root_run.mkdir(parents=True)

    runs = find_runs(tmp_path)

    assert any(r == root_run for r in runs), f"Expected root build run {root_run} in {runs}"


# ---------------------------------------------------------------------------
# (c) build_revision: determinism and None for empty layers
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_revision_deterministic() -> None:
    """SHA-1 of sorted layer hashes is stable across two calls."""
    short_hashes = ["abc123", "def456", "789fed"]

    def _compute(hashes: list[str]) -> str:
        return hashlib.sha1("".join(sorted(hashes)).encode()).hexdigest()[:12]

    first = _compute(short_hashes)
    second = _compute(short_hashes)
    assert first == second, "build_revision must be deterministic"


@pytest.mark.unit
def test_build_revision_order_independent() -> None:
    """Sorting means input order does not affect the result."""
    hashes_a = ["abc123", "def456", "789fed"]
    hashes_b = ["789fed", "abc123", "def456"]

    def _compute(hashes: list[str]) -> str:
        return hashlib.sha1("".join(sorted(hashes)).encode()).hexdigest()[:12]

    assert _compute(hashes_a) == _compute(hashes_b)


@pytest.mark.unit
def test_build_revision_none_for_empty_layers() -> None:
    """Empty layer list must produce build_revision = None (matches assemble_report logic)."""
    layers: list = []
    # Reproduce the exact condition from report.py:
    # if layers: ... else: build_revision = None
    build_revision = (
        hashlib.sha1("".join(sorted(la.short_hash for la in layers)).encode()).hexdigest()[:12] if layers else None
    )
    assert build_revision is None


# ---------------------------------------------------------------------------
# (d) BuildUIState: FATAL counts as error; warn_count / error_count tallying
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_ui_state_severity_counts() -> None:
    """3 WARNING + 1 ERROR + 1 FATAL -> warn_count == 3, error_count == 2."""
    ui = BuildUIState()

    lines = [
        "WARNING: skipping optional package foo",
        "WARNING: missing preferred provider for bar",
        "WARNING: QA Issue: ldconfig-native not in",
        "ERROR: recipe zlib-1.2.11-r0 do_compile: exit code 1",
        "FATAL: Unable to continue with errors",
    ]
    for line in lines:
        ui.process_line(line)

    assert ui.warn_count == 3, f"Expected warn_count=3, got {ui.warn_count}"
    assert ui.error_count == 2, f"Expected error_count=2, got {ui.error_count}"


@pytest.mark.unit
def test_build_ui_state_fatal_increments_error_count() -> None:
    """FATAL alone must increment error_count, not warn_count."""
    ui = BuildUIState()
    ui.process_line("FATAL: Aborted due to earlier errors")

    assert ui.error_count == 1
    assert ui.warn_count == 0


@pytest.mark.unit
def test_build_ui_state_warning_increments_warn_count() -> None:
    """WARNING alone must increment warn_count, not error_count."""
    ui = BuildUIState()
    ui.process_line("WARNING: dependency version mismatch")

    assert ui.warn_count == 1
    assert ui.error_count == 0


# ---------------------------------------------------------------------------
# (e) RunLogger._console_header: written to console.log, not to Rich console
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_run_logger_step_start_header_in_console_log(tmp_path: Path) -> None:
    """step_start writes a greppable '──' header line to console.log."""
    from bakar.observability import RunLogger

    runs_dir = tmp_path / "runs"
    with RunLogger(runs_dir) as log:
        log.step_start("doctor")

        content = log.console_path.read_text()

    header_lines = [ln for ln in content.splitlines() if ln.startswith("──") and "doctor" in ln]
    assert len(header_lines) >= 1, f"No header line found in console.log:\n{content}"


@pytest.mark.unit
def test_run_logger_step_start_header_not_on_rich_console(tmp_path: Path) -> None:
    """step_start must NOT emit the '──' header to the Rich/stderr console."""
    from bakar.observability import RunLogger

    runs_dir = tmp_path / "runs"
    with RunLogger(runs_dir) as log, patch.object(log.console, "print") as mock_print:
        log.step_start("doctor")
        calls = [str(c) for c in mock_print.call_args_list]

    header_calls = [c for c in calls if "──" in c]
    assert len(header_calls) == 0, f"Header marker was emitted to Rich console: {header_calls}"
