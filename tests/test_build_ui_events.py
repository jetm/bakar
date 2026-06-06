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
    _EVT_PARSE_COMPLETED,
    _EVT_PARSE_PROGRESS,
    _EVT_RUNQUEUE_TASK_COMPLETED,
    _EVT_RUNQUEUE_TASK_STARTED,
    _EVT_SCENE_TASK_STARTED,
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

    renderables = list(ui.make_renderable().renderables)
    texts = [r for r in renderables if hasattr(r, "plain")]
    # Rendered as a ratio: pct = int(300 / 320 * 100) = 93.
    sstate = [r for r in texts if "93% sstate (300 cached, 20 will build)" in r.plain]
    assert sstate
    # Order: pipeline header, sstate line, build bar.
    assert renderables.index(sstate[0]) < renderables.index(ui._build_progress)


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


# ---------------------------------------------------------------------------
# Failure freeze protocol -- head line freezes the frame, alert resumes it
# ---------------------------------------------------------------------------


def _failed_stub(logfile: str | None) -> _EventStub:
    e = _EventStub()
    e._package = "glibc-2.39-r0"
    e.taskname = "do_compile"
    e._task = "do_compile"
    if logfile is not None:
        e.logfile = logfile
    return e


def _render(renderable) -> str:
    from rich.console import Console

    con = Console(width=200, force_terminal=False)
    with con.capture() as cap:
        con.print(renderable)
    return cap.get()


_HEAD_LINE = "ERROR: glibc-2.39-r0 do_compile: compile failed"


@pytest.mark.unit
def test_fail_head_line_requests_freeze_once() -> None:
    ui = BuildUIState()
    out = ui.process_line(_HEAD_LINE)
    assert out == _HEAD_LINE  # severity passthrough unchanged
    assert ui.take_fail_freeze() is True
    assert ui._failures == [("glibc-2.39-r0", "do_compile")]
    # Further error lines of the same failure: no second freeze.
    ui.process_line("ERROR: glibc-2.39-r0 do_compile: Execution of 'x' failed with exit code 1")
    assert ui.take_fail_freeze() is False
    assert ui._task_failed_count == 1


@pytest.mark.unit
def test_frozen_frame_collapses_to_status_and_count() -> None:
    ui = BuildUIState()
    e = _runqueue_stub({"total": 450, "setscene_covered": 300, "setscene_total": 320, "setscene_notcovered": 20})
    ui.process_event(_EVT_RUNQUEUE_TASK_STARTED, e)
    ui.process_line(_HEAD_LINE)

    out = _render(ui.make_renderable())
    assert "93% sstate (300 cached, 20 will build)" in out
    assert "1 failed: glibc-2.39-r0:do_compile" in out
    assert "kas_build" not in out  # bar and task table dropped


@pytest.mark.unit
def test_notify_restarted_restores_full_frame() -> None:
    ui = BuildUIState()
    ui.process_event(_EVT_RUNQUEUE_TASK_STARTED, _runqueue_stub({"total": 450}))
    ui.process_line(_HEAD_LINE)
    ui.take_fail_freeze()
    ui.notify_restarted()

    out = _render(ui.make_renderable())
    assert "kas_build" in out  # bar back
    assert "1 failed" in out  # persistent failure summary stays


@pytest.mark.unit
def test_regex_fallback_running_line_requests_restart() -> None:
    ui = BuildUIState()
    ui.process_line(_HEAD_LINE)
    assert ui.take_pending_restart() is False
    ui.process_line("NOTE: Running task 6 of 9005 (/x.bb:do_compile)")
    assert ui.take_pending_restart() is True


@pytest.mark.unit
def test_alert_block_single_failed_line_with_tail(tmp_path: Path) -> None:
    """One ✗ FAILED line per failure - the head line dedupe prevents the
    count from double-incrementing when the event follows the knotty text."""
    log = tmp_path / "do_compile.log"
    log.write_text("compile error here\n")
    ui = BuildUIState(logfile_translator=lambda p: p)
    ui.process_line(_HEAD_LINE)
    ui.process_event(_EVT_TASK_FAILED, _failed_stub(str(log)))

    assert ui._task_failed_count == 1
    assert ui._failures == [("glibc-2.39-r0", "do_compile")]
    alerts = ui.take_pending_alerts()
    assert len(alerts) == 1
    out = _render(alerts[0])
    assert out.count("✗ FAILED") == 1
    assert f"log: {log}" in out
    assert "compile error here" in out
    assert out.index("✗ FAILED") < out.index("compile error here")


@pytest.mark.unit
def test_alert_tail_keeps_last_15_lines(tmp_path: Path) -> None:
    log = tmp_path / "do_compile.log"
    log.write_text("\n".join(f"line {i}" for i in range(20)) + "\n")
    ui = BuildUIState(logfile_translator=lambda p: p)
    ui.process_event(_EVT_TASK_FAILED, _failed_stub(str(log)))
    out = _render(ui.take_pending_alerts()[0])
    # Only the last 15 lines are kept (deque maxlen=15).
    assert "line 5" in out
    assert "line 4" not in out
    assert "line 19" in out


@pytest.mark.unit
def test_alert_missing_file_does_not_raise(tmp_path: Path) -> None:
    ui = BuildUIState(logfile_translator=lambda p: p)
    missing = str(tmp_path / "absent.log")
    # OSError on the unreadable path must be swallowed; the alert still
    # queues, just without a tail.
    ui.process_event(_EVT_TASK_FAILED, _failed_stub(missing))
    out = _render(ui.take_pending_alerts()[0])
    assert "FAILED" in out


@pytest.mark.unit
def test_alert_tail_skipped_without_translator(tmp_path: Path) -> None:
    log = tmp_path / "do_compile.log"
    log.write_text("some output\n")
    ui = BuildUIState()  # no translator
    ui.process_event(_EVT_TASK_FAILED, _failed_stub(str(log)))
    out = _render(ui.take_pending_alerts()[0])
    assert "FAILED" in out
    assert "some output" not in out


@pytest.mark.unit
def test_had_task_failures_property() -> None:
    ui = BuildUIState()
    assert ui.had_task_failures is False
    ui.process_line(_HEAD_LINE)
    assert ui.had_task_failures is True


@pytest.mark.unit
def test_failed_final_frame_collapses_to_header_and_sstate(tmp_path: Path) -> None:
    """finish_failed() keeps only the pipeline header and the sstate line -
    used when the build fails without a recorded task failure."""
    ui = BuildUIState()
    e = _runqueue_stub({"total": 450, "setscene_covered": 300, "setscene_total": 320, "setscene_notcovered": 20})
    ui.process_event(_EVT_RUNQUEUE_TASK_STARTED, e)
    ui.finish_failed()

    out = _render(ui.make_renderable())
    assert "93% sstate (300 cached, 20 will build)" in out
    assert "parse" in out  # breadcrumb present
    assert "kas_build" not in out
    assert "failed" not in out


# ---------------------------------------------------------------------------
# Counter routing -- completion events route through _update_build and feed the bar
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_runqueue_completed_feeds_build_bar() -> None:
    ui = BuildUIState()
    e = _runqueue_stub({"completed": 100, "total": 1000})
    ui.process_event(_EVT_RUNQUEUE_TASK_COMPLETED, e)
    assert ui._build_progress.tasks[0].completed == 100


# ---------------------------------------------------------------------------
# Failure alert -- queued on TaskFailed, drained swap-style
# ---------------------------------------------------------------------------


def _failed_recipe_stub(recipe: str, task: str) -> _EventStub:
    e = _EventStub()
    e._package = recipe
    e.taskname = task
    e._task = task
    return e


@pytest.mark.unit
def test_task_failed_queues_alert_and_drains() -> None:
    ui = BuildUIState()
    ui.process_event(_EVT_TASK_FAILED, _failed_recipe_stub("glibc", "do_compile"))
    alerts = ui.take_pending_alerts()
    assert len(alerts) == 1
    out = _render(alerts[0])
    assert "FAILED" in out
    assert "glibc" in out
    # Swap-drain: a second take returns the empty list.
    assert ui.take_pending_alerts() == []


# ---------------------------------------------------------------------------
# Failure counter -- _task_failed_count and the "N failed" render
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_task_failed_count_and_render() -> None:
    ui = BuildUIState()
    ui.process_event(_EVT_RUNQUEUE_TASK_STARTED, _runqueue_stub({"total": 450}))
    ui.process_event(_EVT_TASK_FAILED, _failed_recipe_stub("glibc", "do_compile"))
    ui.process_event(_EVT_TASK_FAILED, _failed_recipe_stub("busybox", "do_compile"))
    assert ui._task_failed_count == 2
    assert ui._phase is _Phase.BUILD

    texts = [getattr(r, "plain", "") for r in ui.make_renderable().renderables]
    assert any("2 failed" in t for t in texts)


# ---------------------------------------------------------------------------
# Parse cache note -- ParseCompleted reports cached vs parsed in the pending log
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_parse_completed_cache_note() -> None:
    ui = BuildUIState()
    # Stamp _parse_start via a parse-progress event first.
    parse = _EventStub()
    parse.current = 10
    parse.total = 100
    ui.process_event(_EVT_PARSE_PROGRESS, parse)

    done = _EventStub()
    done.cached = 1840
    done.parsed = 42
    ui.process_event(_EVT_PARSE_COMPLETED, done)
    note = ui.take_pending_log()
    assert note is not None
    assert "cached" in note
    assert "42" in note


# ---------------------------------------------------------------------------
# Breadcrumb -- current segment advances parse -> setscene -> build
# ---------------------------------------------------------------------------


def _header_text(ui: BuildUIState) -> str:
    from rich.console import Console

    con = Console(width=110, force_terminal=False)
    with con.capture() as cap:
        con.print(ui._render_breadcrumb())
    return cap.get()


@pytest.mark.unit
def test_breadcrumb_advances_with_phase() -> None:
    ui = BuildUIState()
    # SETUP: parse is active (no check yet), setscene is a future dot. The
    # tasks segment is absent until real tasks actually run - an sstate-warm
    # build never reaches it, so it must not be advertised as queued.
    assert ui._phase is _Phase.SETUP
    out = _header_text(ui)
    assert "✓" not in out
    assert "○ setscene" in out
    assert "tasks" not in out

    # A setscene task completes parse and moves the active marker to setscene.
    scene = _EventStub()
    scene.taskname = "do_fetch_setscene"
    scene.taskfile = "/path/to/glibc.bb"
    ui.process_event(_EVT_SCENE_TASK_STARTED, scene)
    out = _header_text(ui)
    assert "✓ parse" in out
    assert "tasks" not in out

    # A real runqueue task makes the tasks segment appear, active. setscene
    # keeps its spinner: bitbake's merged run queue interleaves the two, so
    # both segments spin until sceneQueueComplete reports the queue drained.
    ui.process_event(_EVT_RUNQUEUE_TASK_STARTED, _runqueue_stub({"total": 450}))
    out = _header_text(ui)
    assert "✓ parse" in out
    assert "✓ setscene" not in out
    assert "tasks" in out
    assert "✓ tasks" not in out and "○ tasks" not in out

    # sceneQueueComplete drains the scene queue: setscene checks, tasks spin on.
    ui.process_event("bb.runqueue.sceneQueueComplete", _EventStub())
    out = _header_text(ui)
    assert "✓ setscene" in out
    assert "✓ tasks" not in out


@pytest.mark.unit
def test_finish_checks_reached_segments_with_durations() -> None:
    """finish() freezes the final frame: every reached segment checked, the
    completed stages carrying their wall-clock duration."""
    import time as _time

    ui = BuildUIState()
    with ui._lock:
        ui._parse_start = _time.monotonic() - 51
    done = _EventStub()
    done.cached = 900
    done.parsed = 10
    ui.process_event("bb.event.ParseCompleted", done)
    scene = _EventStub()
    scene.taskname = "do_fetch_setscene"
    scene.taskfile = "/path/to/glibc.bb"
    ui.process_event(_EVT_SCENE_TASK_STARTED, scene)
    with ui._lock:
        ui._scene_started_at -= 122
    ui.process_event(_EVT_RUNQUEUE_TASK_STARTED, _runqueue_stub({"total": 450}))
    ui.finish()
    out = _header_text(ui)
    assert "✓ parse (51s)" in out
    assert "✓ setscene (2m02s)" in out
    assert "✓ tasks (" in out


@pytest.mark.unit
def test_finish_cached_build_ends_at_setscene() -> None:
    """A fully sstate-cached build never runs real tasks: finish() checks
    setscene with its duration and the tasks segment stays absent."""
    ui = BuildUIState()
    scene = _EventStub()
    scene.taskname = "do_fetch_setscene"
    scene.taskfile = "/path/to/glibc.bb"
    ui.process_event(_EVT_SCENE_TASK_STARTED, scene)
    ui.finish()
    out = _header_text(ui)
    assert "✓ parse" in out
    assert "✓ setscene (" in out
    assert "tasks" not in out


# ---------------------------------------------------------------------------
# Global timer -- start_monotonic seeds the pipeline-header wall clock
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_header_timer_seeded_from_start_monotonic() -> None:
    import time as _time

    ui = BuildUIState(start_monotonic=_time.monotonic() - 154.0)
    out = _header_text(ui)
    assert "2m34s" in out
