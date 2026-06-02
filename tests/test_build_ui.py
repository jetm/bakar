"""Unit tests for ``bakar.steps.build_ui``.

All tests operate on ``BuildUIState`` directly — no subprocess, no PTY, no Rich
console rendering required. The module under test is a pure state machine that
parses knotty PTY output and returns passthrough strings for severity lines.
"""

from __future__ import annotations

import pytest

from bakar.steps.build_ui import BuildUIState, _elapsed_secs, _RunTask

# ---------------------------------------------------------------------------
# CURRENT_RUNNING regex — progress updates and slot pruning
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_current_running_updates_progress() -> None:
    ui = BuildUIState()
    result = ui.process_line("Currently  4 running tasks (120 of 450)  22% |###|")
    assert result is None
    assert ui.progress.tasks[0].completed == 120
    assert ui.progress.tasks[0].total == 450


@pytest.mark.unit
def test_current_running_prunes_stale_slots() -> None:
    ui = BuildUIState()
    # Seed _running with slots 0, 1, 2 directly
    ui._running[0] = _RunTask(slot=0, pf="pkg-a-1.0-r0", task="do_compile", elapsed="10s")
    ui._running[1] = _RunTask(slot=1, pf="pkg-b-2.0-r0", task="do_fetch", elapsed="5s")
    ui._running[2] = _RunTask(slot=2, pf="pkg-c-3.0-r0", task="do_install", elapsed="2s")

    # running_n=1 means only slot 0 is active
    ui.process_line("Currently  1 running tasks (200 of 450)  44% |##########|")

    assert len(ui._running) == 1
    assert 0 in ui._running


@pytest.mark.unit
def test_current_running_expansion_message() -> None:
    ui = BuildUIState()
    ui._last_total = 450
    # total=480 is a 6.7% increase — above the 5% threshold
    result = ui.process_line("Currently  4 running tasks (480 of 480)  100% |####################|")
    assert result is not None
    assert "expanded" in result


@pytest.mark.unit
def test_current_running_reduction_message() -> None:
    ui = BuildUIState()
    ui._last_total = 480
    # total=450 is a 6.25% decrease — above the 5% threshold
    result = ui.process_line("Currently  4 running tasks (450 of 450)  100% |####################|")
    assert result is not None
    assert "reduced" in result


@pytest.mark.unit
def test_current_running_no_expansion_below_threshold() -> None:
    ui = BuildUIState()
    ui._last_total = 450
    # total=451 is a 0.22% change — below the 5% threshold
    result = ui.process_line("Currently  4 running tasks (451 of 451)  100% |####################|")
    assert result is None


# ---------------------------------------------------------------------------
# SETSCENE_RUNNING regex
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_setscene_line() -> None:
    ui = BuildUIState()
    result = ui.process_line("Setscene tasks: 89 of 120")
    assert result is None
    assert ui._setscene_total == 120
    assert ui._setscene.tasks[0].completed == 89


# ---------------------------------------------------------------------------
# KNOTTY_TASK_RE regex — per-task footer lines
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_knotty_task_with_elapsed() -> None:
    ui = BuildUIState()
    result = ui.process_line("0: glibc-2.39-r0 do_compile - 1h2m5s (pid 12345)")
    assert result is None
    assert ui._running[0].pf == "glibc-2.39-r0"
    assert ui._running[0].task == "do_compile"
    assert ui._running[0].elapsed == "1h2m5s"


@pytest.mark.unit
def test_knotty_task_no_elapsed() -> None:
    ui = BuildUIState()
    result = ui.process_line("2: python3-3.12.0-r0 do_configure (pid 99)")
    assert result is None
    assert ui._running[2].elapsed == ""


@pytest.mark.unit
def test_knotty_task_slot_update() -> None:
    ui = BuildUIState()
    ui.process_line("0: glibc-2.39-r0 do_compile - 10s (pid 12345)")
    ui.process_line("0: glibc-2.39-r0 do_compile - 1m5s (pid 12345)")
    assert ui._running[0].elapsed == "1m5s"


# ---------------------------------------------------------------------------
# SEVERITY_PASSTHROUGH regex
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_severity_error_passthrough() -> None:
    line = "ERROR: do_compile failed"
    ui = BuildUIState()
    assert ui.process_line(line) == line


@pytest.mark.unit
def test_severity_warning_passthrough() -> None:
    ui = BuildUIState()
    result = ui.process_line("WARNING: unused variable x")
    assert result is not None


@pytest.mark.unit
def test_severity_fatal_passthrough() -> None:
    ui = BuildUIState()
    result = ui.process_line("FATAL: out of disk")
    assert result is not None


@pytest.mark.unit
def test_severity_qa_issue_passthrough() -> None:
    ui = BuildUIState()
    result = ui.process_line("QA Issue: file not found in expected location")
    assert result is not None


@pytest.mark.unit
def test_unrecognized_line_returns_none() -> None:
    ui = BuildUIState()
    assert ui.process_line("NOTE: some log line") is None


# ---------------------------------------------------------------------------
# _elapsed_secs helper
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_elapsed_secs_seconds() -> None:
    assert _elapsed_secs("47s") == 47


@pytest.mark.unit
def test_elapsed_secs_minutes() -> None:
    assert _elapsed_secs("2m15s") == 135


@pytest.mark.unit
def test_elapsed_secs_hours() -> None:
    assert _elapsed_secs("1h2m5s") == 3725


@pytest.mark.unit
def test_elapsed_secs_empty() -> None:
    assert _elapsed_secs("") == 0


@pytest.mark.unit
def test_elapsed_secs_invalid() -> None:
    assert _elapsed_secs("bogus") == 0


# ---------------------------------------------------------------------------
# make_renderable — Group composition
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_make_renderable_no_tasks_no_setscene() -> None:
    ui = BuildUIState()
    group = ui.make_renderable()
    # Only main progress bar — no setscene, no table
    assert len(group.renderables) == 1


@pytest.mark.unit
def test_make_renderable_with_setscene() -> None:
    ui = BuildUIState()
    ui._setscene_total = 120
    group = ui.make_renderable()
    # Main progress bar + setscene bar
    assert len(group.renderables) == 2


@pytest.mark.unit
def test_make_renderable_with_tasks() -> None:
    # 3 running tasks, setscene_total=0 → bar + table (2 components)
    ui = BuildUIState()

    ui._running[0] = _RunTask(slot=0, pf="pkg-a-1.0-r0", task="do_compile", elapsed="10s")
    ui._running[1] = _RunTask(slot=1, pf="pkg-b-2.0-r0", task="do_fetch", elapsed="5s")
    ui._running[2] = _RunTask(slot=2, pf="pkg-c-3.0-r0", task="do_install", elapsed="2s")

    group = ui.make_renderable()
    # Main progress + table (no setscene)
    assert len(group.renderables) == 2


@pytest.mark.unit
def test_make_renderable_task_sort_by_elapsed_desc() -> None:
    ui = BuildUIState()

    # Seed in non-sorted order
    ui._running[0] = _RunTask(slot=0, pf="pkg-a-1.0-r0", task="do_compile", elapsed="47s")
    ui._running[1] = _RunTask(slot=1, pf="pkg-b-2.0-r0", task="do_fetch", elapsed="2m15s")
    ui._running[2] = _RunTask(slot=2, pf="pkg-c-3.0-r0", task="do_install", elapsed="1h2m5s")

    group = ui.make_renderable()
    # Last renderable is the Table
    table = group.renderables[-1]
    # Column index 3 is elapsed; _cells gives values in row order
    elapsed_cells = table.columns[3]._cells
    assert elapsed_cells[0] == "1h2m5s", f"Expected 1h2m5s first, got {elapsed_cells}"


@pytest.mark.unit
def test_make_renderable_task_removes_do_prefix() -> None:
    ui = BuildUIState()

    ui._running[0] = _RunTask(slot=0, pf="glibc-2.39-r0", task="do_compile", elapsed="5s")

    group = ui.make_renderable()
    table = group.renderables[-1]
    # Column index 2 is task; do_ prefix is stripped
    task_cells = table.columns[2]._cells
    assert task_cells[0] == "compile"


# ---------------------------------------------------------------------------
# update_heartbeat — stall and du field updates
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_update_heartbeat_stall_format() -> None:
    ui = BuildUIState()
    ui.update_heartbeat(47, 0)
    assert ui.progress.tasks[0].fields["stall"] == "47s"


@pytest.mark.unit
def test_update_heartbeat_du_format() -> None:
    ui = BuildUIState()
    ui.update_heartbeat(0, 220_000_000)
    du = ui.progress.tasks[0].fields["du"]
    assert du.startswith("+")
    assert "M" in du
