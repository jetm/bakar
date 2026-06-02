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

from bakar.steps.build_ui import BuildUIState, _Phase, _RunTask

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
    group = ui.make_renderable()
    assert len(group.renderables) == 1
    assert group.renderables[0] is ui._setup_progress


# ---------------------------------------------------------------------------
# BUILD phase transition — Running [setscene] task N of M
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_running_setscene_transitions_to_build() -> None:
    ui = BuildUIState()
    result = ui.process_line("NOTE: Running setscene task 16 of 5944 (/x.bb:do_create_runtime_spdx_setscene)")
    assert result is None
    assert ui._phase == _Phase.BUILD
    assert ui._build_progress.tasks[0].completed == 16
    assert ui._build_progress.tasks[0].total == 5944
    assert ui._build_progress.tasks[0].fields["kind"] == "setscene"


@pytest.mark.unit
def test_running_task_sets_tasks_kind() -> None:
    ui = BuildUIState()
    result = ui.process_line("NOTE: Running task 1200 of 9005 (/x.bb:do_compile)")
    assert result is None
    assert ui._build_progress.tasks[0].completed == 1200
    assert ui._build_progress.tasks[0].total == 9005
    assert ui._build_progress.tasks[0].fields["kind"] == "tasks"


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

    group = ui.make_renderable()
    assert len(group.renderables) == 2


@pytest.mark.unit
def test_make_renderable_build_empty_running() -> None:
    ui = BuildUIState()
    ui.process_line("NOTE: Running task 1200 of 9005 (/x.bb:do_compile)")
    group = ui.make_renderable()
    assert len(group.renderables) == 1
    assert group.renderables[0] is ui._build_progress


@pytest.mark.unit
def test_make_renderable_sort_by_elapsed_desc() -> None:
    ui = BuildUIState()
    ui.process_line("NOTE: Running task 1200 of 9005 (/x.bb:do_compile)")
    base = time.monotonic()
    ui._running["a:do_compile"] = _RunTask(pf="pkg-a-1.0-r0", task="do_compile", start=base - 5)
    ui._running["b:do_fetch"] = _RunTask(pf="pkg-b-2.0-r0", task="do_fetch", start=base - 60)
    ui._running["c:do_install"] = _RunTask(pf="pkg-c-3.0-r0", task="do_install", start=base - 120)

    group = ui.make_renderable()
    table = group.renderables[-1]
    pf_cells = table.columns[0]._cells
    assert pf_cells[0] == "pkg-c-3.0-r0", f"Expected base-120 task first, got {pf_cells}"
    assert pf_cells[-1] == "pkg-a-1.0-r0", f"Expected base-5 task last, got {pf_cells}"


@pytest.mark.unit
def test_make_renderable_strips_do_prefix() -> None:
    ui = BuildUIState()
    ui.process_line("NOTE: Running task 1200 of 9005 (/x.bb:do_compile)")
    ui._running["glibc:do_compile"] = _RunTask(pf="glibc-2.39-r0", task="do_compile", start=time.monotonic())

    group = ui.make_renderable()
    table = group.renderables[-1]
    task_cells = table.columns[1]._cells
    assert task_cells[0] == "compile"


# ---------------------------------------------------------------------------
# update_heartbeat — stall and du field updates on the build bar
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_update_heartbeat_stall_format() -> None:
    ui = BuildUIState()
    ui.update_heartbeat(47, 0)
    assert ui._build_progress.tasks[0].fields["stall"] == "47s"


@pytest.mark.unit
def test_update_heartbeat_du_format() -> None:
    ui = BuildUIState()
    ui.update_heartbeat(0, 220_000_000)
    du = ui._build_progress.tasks[0].fields["du"]
    assert du.startswith("+")
