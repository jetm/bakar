"""Unit tests for ``bakar.steps.build_ui``.

All tests operate on ``BuildUIState`` directly — no subprocess, no PTY, no Rich
console rendering required. The module under test parses knotty's non-interactive
fallback output lines, drives a SETUP/BUILD phase state machine, reconstructs the
live running-task set from lifecycle events, and returns passthrough strings for
severity lines.
"""

from __future__ import annotations

import time

import pytest

from bakar.steps.build_ui import (
    BuildUIState,
    _Phase,
    _RunTask,
    _stuck_color,
    _task_style,
)

# ---------------------------------------------------------------------------
# SETUP phase — parse and cache progress
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_parse_progress_updates_setup_bar() -> None:
    ui = BuildUIState()
    result = ui.process_line("Parsing recipes:  47% || ETA:  0:00:28")
    assert result is None
    assert ui._setup_progress.tasks[0].completed == 47


@pytest.mark.unit
def test_loading_cache_updates_setup_bar() -> None:
    ui = BuildUIState()
    result = ui.process_line("Loading cache: 100% || ETA:  --:--:--")
    assert result is None
    assert ui._setup_progress.tasks[0].completed == 100


@pytest.mark.unit
def test_setup_phase_render_only_setup_bar() -> None:
    ui = BuildUIState()
    inner = ui.make_renderable().renderables
    assert len(inner) == 1
    assert inner[0] is ui._setup_progress


# ---------------------------------------------------------------------------
# BUILD phase transition — Running [setscene] task N of M
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_running_setscene_transitions_to_build() -> None:
    ui = BuildUIState()
    # The first build line flips to BUILD; the passthrough return stays None
    # (the parse-complete message is queued for the caller's logger instead).
    result = ui.process_line("NOTE: Running setscene task 16 of 5944 (/x.bb:do_create_runtime_spdx_setscene)")
    assert result is None
    assert ui._phase == _Phase.BUILD
    assert ui._build_progress.tasks[0].completed == 16
    assert ui._build_progress.tasks[0].total == 5944
    assert ui._build_progress.tasks[0].fields["kind"] == "setscene"


@pytest.mark.unit
def test_running_task_sets_tasks_kind() -> None:
    ui = BuildUIState()
    ui.process_line("NOTE: Running task 1200 of 9005 (/x.bb:do_compile)")
    assert ui._build_progress.tasks[0].completed == 1200
    assert ui._build_progress.tasks[0].total == 9005
    assert ui._build_progress.tasks[0].fields["kind"] == "tasks"


@pytest.mark.unit
def test_parse_complete_queued_with_check_and_duration_once() -> None:
    ui = BuildUIState()
    # A parse line stamps the parse start, so completion reports a duration.
    ui.process_line("Parsing recipes:  10% || ETA:  0:00:30")
    ui.process_line("NOTE: Running setscene task 1 of 5944 (/x.bb:do_x_setscene)")
    pending = ui.take_pending_log()
    assert pending is not None
    assert "✓" in pending  # the completion check icon
    assert "parsing recipes complete" in pending
    assert "(" in pending and "s)" in pending  # the elapsed duration, e.g. "(3s)"
    # The message is one-shot: draining it clears it.
    assert ui.take_pending_log() is None
    # A second Running line must NOT re-queue it.
    ui.process_line("NOTE: Running setscene task 2 of 5944 (/x.bb:do_x_setscene)")
    assert ui.take_pending_log() is None


@pytest.mark.unit
def test_global_timer_backdated_to_bakar_start() -> None:
    start = time.monotonic() - 100.0
    ui = BuildUIState(start_monotonic=start)
    # The build task's clock is seeded from the bakar start stamp, so the global
    # timer includes the pre-build time (doctor, sync, parse), not just the build.
    assert ui._build_progress.tasks[0].start_time == start


# ---------------------------------------------------------------------------
# Running-task set reconstruction — Started / Succeeded / Failed
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_recipe_started_adds_running() -> None:
    ui = BuildUIState()
    result = ui.process_line("NOTE: recipe go-binary-native-1.22.12-r0: task do_compile: Started")
    assert result is None
    assert len(ui._running) == 1
    entry = next(iter(ui._running.values()))
    assert entry.pf == "go-binary-native-1.22.12-r0"
    assert entry.task == "do_compile"


@pytest.mark.unit
def test_recipe_succeeded_removes_running() -> None:
    ui = BuildUIState()
    ui.process_line("NOTE: recipe go-binary-native-1.22.12-r0: task do_compile: Started")
    ui.process_line("NOTE: recipe go-binary-native-1.22.12-r0: task do_compile: Succeeded")
    assert ui._running == {}


@pytest.mark.unit
def test_recipe_failed_removes_running() -> None:
    ui = BuildUIState()
    ui.process_line("NOTE: recipe go-binary-native-1.22.12-r0: task do_compile: Started")
    ui.process_line("NOTE: recipe go-binary-native-1.22.12-r0: task do_compile: Failed")
    assert ui._running == {}


@pytest.mark.unit
def test_recipe_started_setscene_task() -> None:
    ui = BuildUIState()
    ui.process_line("NOTE: recipe go-binary-native-1.22.12-r0: task do_create_runtime_spdx_setscene: Started")
    assert len(ui._running) == 1
    entry = next(iter(ui._running.values()))
    assert entry.task == "do_create_runtime_spdx_setscene"


# ---------------------------------------------------------------------------
# Fallback-mode detection
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_fallback_mode_sets_flag() -> None:
    ui = BuildUIState()
    result = ui.process_line("NOTE: Unable to use interactive mode for this terminal, using fallback")
    assert result is None
    assert ui.fallback_detected is True


# ---------------------------------------------------------------------------
# Severity passthrough
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_severity_error_passthrough() -> None:
    line = "ERROR: do_compile failed for glibc"
    ui = BuildUIState()
    assert ui.process_line(line) == line


@pytest.mark.unit
def test_severity_warning_passthrough() -> None:
    ui = BuildUIState()
    result = ui.process_line("WARNING: x")
    assert result is not None


@pytest.mark.unit
def test_unrecognized_line_returns_none() -> None:
    ui = BuildUIState()
    assert ui.process_line("NOTE: some log line") is None


# ---------------------------------------------------------------------------
# make_renderable — BUILD phase Group composition
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_make_renderable_build_with_tasks() -> None:
    ui = BuildUIState()
    ui.process_line("NOTE: Running task 1200 of 9005 (/x.bb:do_compile)")
    base = time.monotonic()
    ui._running["a:do_compile"] = _RunTask(pf="pkg-a-1.0-r0", task="do_compile", start=base - 5)
    ui._running["b:do_fetch"] = _RunTask(pf="pkg-b-2.0-r0", task="do_fetch", start=base - 60)
    ui._running["c:do_install"] = _RunTask(pf="pkg-c-3.0-r0", task="do_install", start=base - 120)

    # Group is [build_progress, table] in the BUILD phase with tasks.
    inner = ui.make_renderable().renderables
    assert len(inner) == 2
    assert inner[0] is ui._build_progress


@pytest.mark.unit
def test_make_renderable_build_empty_running() -> None:
    ui = BuildUIState()
    ui.process_line("NOTE: Running task 1200 of 9005 (/x.bb:do_compile)")
    # No running tasks: Group is just [build_progress].
    inner = ui.make_renderable().renderables
    assert len(inner) == 1
    assert inner[0] is ui._build_progress


@pytest.mark.unit
def test_make_renderable_sort_by_elapsed_desc() -> None:
    ui = BuildUIState()
    ui.process_line("NOTE: Running task 1200 of 9005 (/x.bb:do_compile)")
    base = time.monotonic()
    ui._running["a:do_compile"] = _RunTask(pf="pkg-a-1.0-r0", task="do_compile", start=base - 5)
    ui._running["b:do_fetch"] = _RunTask(pf="pkg-b-2.0-r0", task="do_fetch", start=base - 60)
    ui._running["c:do_install"] = _RunTask(pf="pkg-c-3.0-r0", task="do_install", start=base - 120)

    table = ui.make_renderable().renderables[-1]
    # Columns: 0=spinner, 1=icon, 2=pf, 3=task, 4=elapsed; cells are Text.
    pf_cells = [c.plain for c in table.columns[2]._cells]
    assert pf_cells[0] == "pkg-c-3.0-r0", f"Expected base-120 task first, got {pf_cells}"
    assert pf_cells[-1] == "pkg-a-1.0-r0", f"Expected base-5 task last, got {pf_cells}"


@pytest.mark.unit
def test_make_renderable_strips_do_prefix() -> None:
    ui = BuildUIState()
    ui.process_line("NOTE: Running task 1200 of 9005 (/x.bb:do_compile)")
    ui._running["glibc:do_compile"] = _RunTask(pf="glibc-2.39-r0", task="do_compile", start=time.monotonic())

    table = ui.make_renderable().renderables[-1]
    task_cells = [c.plain for c in table.columns[3]._cells]
    assert task_cells[0] == "compile"


# ---------------------------------------------------------------------------
# update_heartbeat — retained no-op (must not raise)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_update_heartbeat_is_noop() -> None:
    ui = BuildUIState()
    # No stall/du fields exist on the build bar anymore; the call must not raise.
    ui.update_heartbeat(47, 220_000_000)
    assert "du" not in ui._build_progress.tasks[0].fields
    assert "stall" not in ui._build_progress.tasks[0].fields


# ---------------------------------------------------------------------------
# Graphics helpers — task styling and stuck detection
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_task_style_by_category() -> None:
    assert _task_style("do_compile")[1] == "yellow"
    assert _task_style("do_fetch")[1] == "blue"
    assert _task_style("do_configure")[1] == "cyan"
    assert _task_style("do_package_write_rpm")[1] == "green"
    # setscene wins regardless of the base task name.
    assert _task_style("do_compile_setscene")[1] == "bright_black"


@pytest.mark.unit
def test_stuck_color_thresholds() -> None:
    # Fewer than 3 running tasks: no stuck highlight.
    assert _stuck_color(1000, 10, 2) is None
    # >4x median is red, >2x is yellow, otherwise no highlight.
    assert _stuck_color(50, 10, 5) == "bold red"
    assert _stuck_color(25, 10, 5) == "yellow"
    assert _stuck_color(15, 10, 5) is None


@pytest.mark.unit
def test_global_timer_is_continuous_across_transition() -> None:
    ui = BuildUIState()
    # The global timer is the build task's elapsed column, started at
    # construction. Its start_time must not be reset across the parse->build
    # transition, so it spans parse + build.
    start_time = ui._build_progress.tasks[0].start_time
    ui.process_line("Parsing recipes:  80% || ETA:  0:00:05")
    ui.process_line("NOTE: Running task 5 of 9005 (/x.bb:do_compile)")
    assert ui._build_progress.tasks[0].start_time == start_time
