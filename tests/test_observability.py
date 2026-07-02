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
    with RunLogger(runs_dir) as log, patch.object(log.console, "print") as mock_print:
        log.step_start("kas_build")
        calls = [str(c) for c in mock_print.call_args_list]

    # The Rich console.print should not have been called with the header marker
    header_calls = [c for c in calls if "──" in c]
    assert len(header_calls) == 0, f"header marker was emitted to Rich console: {calls}"


@pytest.mark.unit
def test_persist_bitbake_events_writes_artifact_and_announces(tmp_path: Path) -> None:
    """A run dir with a raw event log produces bitbake-events.json plus one announce."""
    import json
    from pathlib import Path as _Path

    fixture = _Path(__file__).parent / "fixtures" / "bitbake_eventlog.json"
    runs_dir = tmp_path / "runs"
    with RunLogger(runs_dir) as log:
        log.eventlog_path.write_bytes(fixture.read_bytes())
        log.persist_bitbake_events()

        assert log.bitbake_events_path.is_file()
        artifact = json.loads(log.bitbake_events_path.read_text())
        assert set(artifact) >= {"schema_version", "build", "tasks", "setscene", "failures"}

    events = [json.loads(ln) for ln in log.events_path.read_text().splitlines() if ln]
    announce = [e for e in events if e.get("step") == "bitbake_events"]
    assert len(announce) == 1
    assert announce[0]["event"] == "step_ok"
    assert announce[0]["path"] == str(log.bitbake_events_path)


@pytest.mark.unit
def test_persist_bitbake_events_noop_without_raw_log(tmp_path: Path) -> None:
    """No raw log: nothing is written, nothing is announced, no exception raised."""
    import json

    runs_dir = tmp_path / "runs"
    with RunLogger(runs_dir) as log:
        assert not log.eventlog_path.exists()
        log.persist_bitbake_events()

        assert not log.bitbake_events_path.exists()

    events = [json.loads(ln) for ln in log.events_path.read_text().splitlines() if ln]
    announce = [e for e in events if e.get("step") == "bitbake_events"]
    assert announce == []


@pytest.mark.unit
def test_persist_sccache_stats_writes_per_language_keys(tmp_path: Path) -> None:
    """The writer produces readable JSON carrying the per-language keys."""
    import json

    doc = {
        "cache_hits": 52697,
        "cache_misses": 4333,
        "hits_by_lang": {"C/C++": 52186, "Rust": 511},
        "misses_by_lang": {"Assembler": 70, "C/C++": 4263},
        "per_node": {"10.42.0.2": 5107},
    }
    runs_dir = tmp_path / "runs"
    with RunLogger(runs_dir) as log:
        log.persist_sccache_stats(doc)

        assert log.sccache_stats_path.is_file()
        written = json.loads(log.sccache_stats_path.read_text())
        assert written["hits_by_lang"] == {"C/C++": 52186, "Rust": 511}
        assert written["misses_by_lang"] == {"Assembler": 70, "C/C++": 4263}

    events = [json.loads(ln) for ln in log.events_path.read_text().splitlines() if ln]
    announce = [e for e in events if e.get("step") == "sccache_stats"]
    assert len(announce) == 1
    assert announce[0]["event"] == "step_ok"


@pytest.mark.unit
def test_persist_sccache_stats_noop_when_unwritable(tmp_path: Path) -> None:
    """A write failure is swallowed: no raise, and the doc is not written."""
    doc = {"hits_by_lang": {"C/C++": 1}, "misses_by_lang": {}}
    runs_dir = tmp_path / "runs"
    with RunLogger(runs_dir) as log:
        # Make the target path a directory so write_text raises OSError.
        log.sccache_stats_path.mkdir(parents=True, exist_ok=True)
        log.persist_sccache_stats(doc)  # must not raise
        assert log.sccache_stats_path.is_dir()


@pytest.mark.unit
def test_persist_sccache_stats_noop_for_none_doc(tmp_path: Path) -> None:
    """A None doc (no running daemon) writes nothing and does not raise."""
    runs_dir = tmp_path / "runs"
    with RunLogger(runs_dir) as log:
        log.persist_sccache_stats(None)
        assert not log.sccache_stats_path.exists()
