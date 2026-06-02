"""Tests for :mod:`bakar.observability` console-phase-headers and RunLogger.

Focused on the ``_console_header`` mechanism: headers must appear in
``console.log`` but not be emitted to the Rich/stderr console.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from bakar.observability import RunLogger

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.unit
def test_step_start_writes_header_to_console_log(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    with RunLogger(runs_dir) as log:
        log.step_start("kas_build")

        content = log.console_path.read_text()

    assert "kas_build" in content
    # UTC ISO timestamp — look for the 'T' separating date and time
    lines = [ln for ln in content.splitlines() if "kas_build" in ln and ln.startswith("──")]
    assert len(lines) >= 1, f"no header line found in:\n{content}"
    header = lines[0]
    assert "T" in header  # UTC ISO timestamp present


@pytest.mark.unit
def test_step_ok_writes_header_to_console_log(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    with RunLogger(runs_dir) as log:
        log.step_ok("repo_sync")

        content = log.console_path.read_text()

    lines = [ln for ln in content.splitlines() if "repo_sync" in ln and ln.startswith("──")]
    assert len(lines) >= 1


@pytest.mark.unit
def test_step_fail_writes_header_to_console_log(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    with RunLogger(runs_dir) as log:
        log.step_fail("kas_build", "exit code 1")

        content = log.console_path.read_text()

    lines = [ln for ln in content.splitlines() if "kas_build" in ln and ln.startswith("──")]
    assert len(lines) >= 1


@pytest.mark.unit
def test_step_skip_does_not_write_header(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    with RunLogger(runs_dir) as log:
        log.step_skip("repo_sync", "dry-run")

        content = log.console_path.read_text()

    header_lines = [ln for ln in content.splitlines() if ln.startswith("──")]
    assert len(header_lines) == 0


@pytest.mark.unit
def test_events_jsonl_unchanged_by_headers(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    with RunLogger(runs_dir) as log:
        log.step_start("kas_build")
        log.step_ok("kas_build")

    import json

    events = [json.loads(ln) for ln in log.events_path.read_text().splitlines() if ln]
    step_events = [e for e in events if e.get("event") in {"step_start", "step_ok"}]
    assert len(step_events) == 2
    for e in step_events:
        assert set(e.keys()) <= {"ts", "event", "step"}, f"unexpected extra keys in {e}"


@pytest.mark.unit
def test_header_not_emitted_to_rich_console(tmp_path: Path) -> None:
    """The Rich/stderr console must NOT receive the header line."""
    runs_dir = tmp_path / "runs"
    printed: list[str] = []

    with RunLogger(runs_dir) as log:
        # Capture what gets printed to the Rich console object
        with patch.object(log.console, "print") as mock_print:
            log.step_start("kas_build")
            calls = [str(c) for c in mock_print.call_args_list]

    # The Rich console.print should not have been called with the header marker
    header_calls = [c for c in calls if "──" in c]
    assert len(header_calls) == 0, f"header marker was emitted to Rich console: {calls}"
