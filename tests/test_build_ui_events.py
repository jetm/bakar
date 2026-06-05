"""Unit tests for ``BuildUIState.process_event`` -- the event-driven feed.

These exercise the structured-event path that maps decoded bitbake events onto
the input-agnostic render model, plus the feed flip that freezes the knotty-text
regex feed once events arrive. Synthetic events are built with
``eventlog._EventStub`` (attributes set directly); the setscene-stats case reads
the committed fixture's ``runQueueTaskStarted`` line.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bakar.eventlog import _decode_line, _EventStub
from bakar.steps.build_ui import (
    _EVT_CACHE_LOAD_PROGRESS,
    _EVT_PARSE_PROGRESS,
    _EVT_RUNQUEUE_TASK_STARTED,
    _EVT_TASK_FAILED,
    _EVT_TASK_STARTED,
    _EVT_TASK_SUCCEEDED,
    BuildUIState,
    _Phase,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "bitbake_eventlog.json"


def _runqueue_stub(stats: dict | None) -> _EventStub:
    e = _EventStub()
    if stats is not None:
        e.stats = stats
    return e


def _fixture_runqueue_event() -> tuple[str, _EventStub]:
    for line in _FIXTURE.read_text().splitlines():
        decoded = _decode_line(line.strip())
        if decoded is not None and decoded[0] == _EVT_RUNQUEUE_TASK_STARTED:
            return decoded
    raise AssertionError("fixture has no runQueueTaskStarted line")


# ---------------------------------------------------------------------------
# Authoritative total + SETUP -> BUILD transition
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_authoritative_total_from_stats() -> None:
    ui = BuildUIState()
    e = _runqueue_stub({"total": 450, "completed": 0, "active": 0})
    ui.process_event(_EVT_RUNQUEUE_TASK_STARTED, e)
    assert ui._build_progress.tasks[0].total == 450


@pytest.mark.unit
def test_runqueue_transitions_to_build() -> None:
    ui = BuildUIState()
    assert ui._phase is _Phase.SETUP
    ui.process_event(_EVT_RUNQUEUE_TASK_STARTED, _runqueue_stub({"total": 450}))
    assert ui._phase is _Phase.BUILD


@pytest.mark.unit
def test_completed_from_completed_plus_active() -> None:
    ui = BuildUIState()
    e = _runqueue_stub({"total": 450, "completed": 100, "active": 25})
    ui.process_event(_EVT_RUNQUEUE_TASK_STARTED, e)
    assert ui._build_progress.tasks[0].completed == 125


# ---------------------------------------------------------------------------
# stats absent -- total unchanged, no raise
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_stats_absent_leaves_total_unchanged() -> None:
    ui = BuildUIState()
    before = ui._build_progress.tasks[0].total
    # No stats attribute set -> the stub returns None.
    ui.process_event(_EVT_RUNQUEUE_TASK_STARTED, _runqueue_stub(None))
    assert ui._build_progress.tasks[0].total == before


# ---------------------------------------------------------------------------
# Setscene-reuse capture (from the committed fixture)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_setscene_reuse_from_fixture() -> None:
    ui = BuildUIState()
    class_name, event = _fixture_runqueue_event()
    ui.process_event(class_name, event)
    assert ui._setscene_covered == 412
    assert ui._setscene_total == 450
    # The fixture's stats dict carries no ``total``, so the build total is left
    # as it was at construction.
    assert ui._build_progress.tasks[0].total is None


# ---------------------------------------------------------------------------
# Parse / cache progress
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_parse_progress_event() -> None:
    ui = BuildUIState()
    e = _EventStub()
    e.current = 225
    e.total = 450
    ui.process_event(_EVT_PARSE_PROGRESS, e)
    assert ui._setup_progress.tasks[0].completed == 50
    assert ui._setup_progress.tasks[0].fields["stage"] == "parsing recipes"


@pytest.mark.unit
def test_cache_load_progress_event() -> None:
    ui = BuildUIState()
    e = _EventStub()
    e.current = 50
    e.total = 200
    ui.process_event(_EVT_CACHE_LOAD_PROGRESS, e)
    assert ui._setup_progress.tasks[0].completed == 25
    assert ui._setup_progress.tasks[0].fields["stage"] == "loading cache"


# ---------------------------------------------------------------------------
# Task lifecycle -- TaskStarted adds, TaskSucceeded / TaskFailed remove
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_task_started_adds_running_row() -> None:
    ui = BuildUIState()
    e = _EventStub()
    e._package = "glibc-2.39-r0"
    e.taskname = "do_compile"
    e._task = "do_compile"
    ui.process_event(_EVT_TASK_STARTED, e)
    assert "glibc-2.39-r0:do_compile" in ui._running
    entry = ui._running["glibc-2.39-r0:do_compile"]
    assert entry.pf == "glibc-2.39-r0"
    assert entry.task == "do_compile"


@pytest.mark.unit
def test_task_succeeded_removes_running_row() -> None:
    ui = BuildUIState()
    started = _EventStub()
    started._package = "glibc-2.39-r0"
    started.taskname = "do_compile"
    started._task = "do_compile"
    ui.process_event(_EVT_TASK_STARTED, started)

    done = _EventStub()
    done._package = "glibc-2.39-r0"
    done.taskname = "do_compile"
    done._task = "do_compile"
    ui.process_event(_EVT_TASK_SUCCEEDED, done)
    assert ui._running == {}


@pytest.mark.unit
def test_task_failed_removes_running_row() -> None:
    ui = BuildUIState()
    started = _EventStub()
    started._package = "glibc-2.39-r0"
    started.taskname = "do_compile"
    started._task = "do_compile"
    ui.process_event(_EVT_TASK_STARTED, started)

    failed = _EventStub()
    failed._package = "glibc-2.39-r0"
    failed.taskname = "do_compile"
    failed._task = "do_compile"
    ui.process_event(_EVT_TASK_FAILED, failed)
    assert ui._running == {}


# ---------------------------------------------------------------------------
# Feed flip -- regex feed stops mutating the build bar once events arrive
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_regex_updates_total_before_any_event() -> None:
    ui = BuildUIState()
    # No event yet: the knotty regex feed drives the build total.
    ui.process_line("NOTE: Running task 5 of 9005 (/x.bb:do_compile)")
    assert ui._build_progress.tasks[0].total == 9005


@pytest.mark.unit
def test_feed_flip_freezes_build_total_after_event() -> None:
    ui = BuildUIState()
    # One event flips the feed; the event sets the authoritative total.
    ui.process_event(_EVT_RUNQUEUE_TASK_STARTED, _runqueue_stub({"total": 450}))
    assert ui._build_progress.tasks[0].total == 450
    # A subsequent knotty line must NOT overwrite it with the scraped count.
    ui.process_line("NOTE: Running task 5 of 9005 (/x.bb:do_compile)")
    assert ui._build_progress.tasks[0].total == 450


# ---------------------------------------------------------------------------
# Regex fallback -- knotty text alone drives SETUP and BUILD without error
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_regex_fallback_drives_setup_and_build() -> None:
    ui = BuildUIState()
    # No events at all: process_line must still advance through both phases.
    ui.process_line("Parsing recipes:  47% || ETA:  0:00:28")
    assert ui._setup_progress.tasks[0].completed == 47
    assert ui._phase is _Phase.SETUP

    ui.process_line("NOTE: Running task 1200 of 9005 (/x.bb:do_compile)")
    assert ui._phase is _Phase.BUILD
    assert ui._build_progress.tasks[0].total == 9005


# ---------------------------------------------------------------------------
# Severity passthrough + counts while event-driven
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_severity_passthrough_while_event_driven() -> None:
    ui = BuildUIState()
    # Flip the feed first.
    ui.process_event(_EVT_RUNQUEUE_TASK_STARTED, _runqueue_stub({"total": 450}))
    assert ui._event_driven is True

    warn = ui.process_line("WARNING: something off")
    assert warn == "WARNING: something off"
    assert ui.warn_count == 1

    err = ui.process_line("ERROR: do_compile failed")
    assert err == "ERROR: do_compile failed"
    assert ui.error_count == 1


# ---------------------------------------------------------------------------
# Rendered setscene line -- present when covered>0, absent when total==0
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_setscene_line_rendered_when_covered() -> None:
    ui = BuildUIState()
    e = _runqueue_stub({"total": 450, "setscene_covered": 300, "setscene_total": 320, "setscene_notcovered": 20})
    ui.process_event(_EVT_RUNQUEUE_TASK_STARTED, e)
    assert ui._phase is _Phase.BUILD

    texts = [r for r in ui.make_renderable().renderables if hasattr(r, "plain")]
    # Rendered as a ratio: pct = int(300 / 320 * 100) = 93.
    assert any("93% sstate (300 cached, 20 will build)" in r.plain for r in texts)


@pytest.mark.unit
def test_setscene_line_absent_when_total_zero() -> None:
    ui = BuildUIState()
    # Build phase with no setscene stats (setscene_total stays 0).
    ui.process_event(_EVT_RUNQUEUE_TASK_STARTED, _runqueue_stub({"total": 450}))
    assert ui._phase is _Phase.BUILD
    assert ui._setscene_total == 0

    for r in ui.make_renderable().renderables:
        plain = getattr(r, "plain", "")
        assert "sstate cache" not in plain
