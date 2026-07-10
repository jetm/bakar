"""Unit tests for ``bakar.steps.build_ui``.

All tests operate on ``BuildUIState`` directly — no subprocess, no PTY, no Rich
console rendering required. The module under test parses knotty's non-interactive
fallback output lines, drives a SETUP/BUILD phase state machine, reconstructs the
live running-task set from lifecycle events, and returns passthrough strings for
severity lines.
"""

from __future__ import annotations

import json
import os
import time
from typing import TYPE_CHECKING

import pytest
from rich.text import Text

from bakar import cache_render
from bakar.steps.build_ui import (
    _ICON_TIMER,
    BuildUIState,
    _Phase,
    _RunTask,
    _stuck_color,
    _task_style,
)

if TYPE_CHECKING:
    from pathlib import Path

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
    # The pipeline header is always the first element; the setup bar follows.
    assert len(inner) == 2
    assert isinstance(inner[0], Text)
    assert inner[1] is ui._setup_progress


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
def test_fallback_parse_complete_stores_segment_duration() -> None:
    """The regex path must close the parse segment's clock too: when the
    event feed is dead (or loses the race), the breadcrumb's "✓ parse (51s)"
    reads _seg_durations, not the one-shot log message."""
    ui = BuildUIState()
    ui.process_line("Parsing recipes:  10% || ETA:  0:00:30")
    ui.process_line("NOTE: Running setscene task 1 of 5944 (/x.bb:do_x_setscene)")
    assert "parse" in ui._seg_durations


@pytest.mark.unit
def test_global_timer_backdated_to_bakar_start() -> None:
    from rich.console import Console

    start = time.monotonic() - 154.0
    ui = BuildUIState(start_monotonic=start)
    # The global timer lives on the pipeline header and counts from the bakar
    # start stamp, so it includes pre-build time (doctor, sync, parse).
    assert ui._start_monotonic == start
    con = Console(width=110, force_terminal=False)
    with con.capture() as cap:
        con.print(ui.make_renderable())
    out = cap.get()
    assert _ICON_TIMER in out
    assert "2m34s" in out


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

    # Group is [header, build_progress, table] in the BUILD phase with tasks.
    inner = ui.make_renderable().renderables
    assert len(inner) == 3
    assert isinstance(inner[0], Text)
    assert inner[1] is ui._build_progress


@pytest.mark.unit
def test_make_renderable_build_empty_running() -> None:
    ui = BuildUIState()
    ui.process_line("NOTE: Running task 1200 of 9005 (/x.bb:do_compile)")
    # No running tasks: Group is [header, build_progress].
    inner = ui.make_renderable().renderables
    assert len(inner) == 2
    assert isinstance(inner[0], Text)
    assert inner[1] is ui._build_progress


@pytest.mark.unit
def test_make_renderable_sort_by_elapsed_desc() -> None:
    ui = BuildUIState()
    ui.process_line("NOTE: Running task 1200 of 9005 (/x.bb:do_compile)")
    base = time.monotonic()
    ui._running["a:do_compile"] = _RunTask(pf="pkg-a-1.0-r0", task="do_compile", start=base - 5)
    ui._running["b:do_fetch"] = _RunTask(pf="pkg-b-2.0-r0", task="do_fetch", start=base - 60)
    ui._running["c:do_install"] = _RunTask(pf="pkg-c-3.0-r0", task="do_install", start=base - 120)

    table = ui.make_renderable().renderables[-1]
    # Columns: 0=spinner, 1=icon, 2=cache-backend badge, 3=pf, 4=task, 5=elapsed; cells are Text.
    pf_cells = [c.plain for c in table.columns[3]._cells]
    assert pf_cells[0] == "pkg-c-3.0-r0", f"Expected base-120 task first, got {pf_cells}"
    assert pf_cells[-1] == "pkg-a-1.0-r0", f"Expected base-5 task last, got {pf_cells}"


@pytest.mark.unit
def test_make_renderable_strips_do_prefix() -> None:
    ui = BuildUIState()
    ui.process_line("NOTE: Running task 1200 of 9005 (/x.bb:do_compile)")
    ui._running["glibc:do_compile"] = _RunTask(pf="glibc-2.39-r0", task="do_compile", start=time.monotonic())

    table = ui.make_renderable().renderables[-1]
    task_cells = [c.plain for c in table.columns[4]._cells]
    assert task_cells[0] == "compile"


@pytest.mark.unit
def test_make_renderable_column_widths_never_shrink() -> None:
    """Columns hold a static position when a long-named recipe finishes.

    Widths are high-water marks: after the widest recipe leaves the running
    set, the pf column must keep its width so the task and elapsed columns
    do not jump left between frames.
    """
    ui = BuildUIState()
    ui.process_line("NOTE: Running task 1200 of 9005 (/x.bb:do_compile)")
    base = time.monotonic()
    long_pf = "lib32-packagegroup-core-x11-base-extended-1.0-r0"
    ui._running["a:do_compile"] = _RunTask(pf=long_pf, task="do_compile", start=base - 60)
    ui._running["b:do_fetch"] = _RunTask(pf="tiny-1.0-r0", task="do_fetch", start=base - 5)

    table = ui.make_renderable().renderables[-1]
    wide = table.columns[3].width
    assert wide == len(long_pf), "pf column must fit the longest recipe untruncated"

    del ui._running["a:do_compile"]
    table = ui.make_renderable().renderables[-1]
    assert table.columns[3].width == wide, "pf column must not shrink after the long recipe finishes"


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
def test_stuck_color_estimated_bypasses_count_guard() -> None:
    # With an estimated baseline, the count/median guard is bypassed: a single
    # running task still highlights when it overruns its historical mean.
    assert _stuck_color(50, 0, 1, estimated=10) == "bold red"
    assert _stuck_color(25, 0, 1, estimated=10) == "yellow"
    assert _stuck_color(15, 0, 1, estimated=10) is None
    # A non-positive estimate falls back to the median path (here: guarded out).
    assert _stuck_color(1000, 10, 2, estimated=0) is None
    # estimated=None keeps the median-based behavior unchanged.
    assert _stuck_color(50, 10, 5, estimated=None) == "bold red"
    assert _stuck_color(1000, 10, 2, estimated=None) is None


@pytest.mark.unit
def test_global_timer_is_continuous_across_transition() -> None:
    ui = BuildUIState()
    # The global timer derives from one immutable stamp on the header, so it
    # cannot reset across the parse->build transition.
    stamp = ui._start_monotonic
    ui.process_line("Parsing recipes:  80% || ETA:  0:00:05")
    ui.process_line("NOTE: Running task 5 of 9005 (/x.bb:do_compile)")
    assert ui._start_monotonic == stamp


@pytest.mark.unit
def test_estimate_never_rendered_but_colors_stuck_tasks() -> None:
    from rich.console import Console

    ui = BuildUIState()
    ui._phase = _Phase.BUILD
    # 540s elapsed vs 235s baseline (>2x, <4x) -> yellow from the baseline
    # even with a single running task (median guard bypassed). No drift
    # indicator below red.
    ui._running["glibc-2.39-r0:do_compile"] = _RunTask(
        pf="glibc-2.39-r0", task="do_compile", start=time.monotonic() - 540, estimated=235.0
    )
    con = Console(width=100, force_terminal=False)
    with con.capture() as cap:
        con.print(ui.make_renderable())
    lines = [ln for ln in cap.get().splitlines() if "glibc-2.39-r0" in ln]
    # One row per task; the noisy per-row estimate is deliberately not shown -
    # the baseline only feeds the stuck-task coloring.
    assert len(lines) == 1
    assert "9m00s" in lines[0]
    assert "est" not in lines[0]
    assert "+" not in lines[0]


@pytest.mark.unit
def test_red_stuck_task_shows_drift_over_reference() -> None:
    from rich.console import Console

    ui = BuildUIState()
    ui._phase = _Phase.BUILD
    # 1000s elapsed vs 235s baseline (>4x) -> red, with a warning-iconed
    # second timer showing the overrun: 1000 - 235 = 765s = 12m45s.
    ui._running["glibc-2.39-r0:do_compile"] = _RunTask(
        pf="glibc-2.39-r0", task="do_compile", start=time.monotonic() - 1000, estimated=235.0
    )
    con = Console(width=100, force_terminal=False)
    with con.capture() as cap:
        con.print(ui.make_renderable())
    lines = [ln for ln in cap.get().splitlines() if "glibc-2.39-r0" in ln]
    assert len(lines) == 1
    assert "16m40s" in lines[0]
    assert "+12m45s" in lines[0]


@pytest.mark.unit
def test_task_table_capped_with_overflow_line() -> None:
    from rich.console import Console

    ui = BuildUIState()
    ui._phase = _Phase.BUILD
    base = time.monotonic()
    for i in range(20):
        ui._running[f"pkg-{i:02d}:do_compile"] = _RunTask(
            pf=f"pkg-{i:02d}-1.0-r0", task="do_compile", start=base - (i + 1)
        )
    con = Console(width=110, force_terminal=False)
    with con.capture() as cap:
        con.print(ui.make_renderable())
    out = cap.get()
    rows = [ln for ln in out.splitlines() if "-1.0-r0" in ln]
    # 20 running tasks render at most _MAX_TASK_ROWS rows plus an overflow line.
    assert len(rows) == 16
    assert "+4 more running" in out
    # Longest-elapsed first: pkg-19 (oldest start) visible, pkg-00 dropped.
    assert "pkg-19-1.0-r0" in out
    assert "pkg-00-1.0-r0" not in out


# ---------------------------------------------------------------------------
# stall_report — wedged-task detection via running-task log freshness
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_stall_report_none_when_no_running_tasks() -> None:
    """Nothing running -> nothing to judge."""
    assert BuildUIState().stall_report() is None


@pytest.mark.unit
def test_stall_report_none_when_running_task_has_no_logfile() -> None:
    """A running task without a captured logfile is unjudgeable, not a stall."""
    ui = BuildUIState()
    ui._running["u-boot:do_compile"] = _RunTask(pf="u-boot", task="do_compile", start=time.monotonic())
    assert ui.stall_report() is None


@pytest.mark.unit
def test_stall_report_none_when_logfile_missing(tmp_path) -> None:
    """An unreadable logfile path is skipped, not treated as infinitely stale."""
    ui = BuildUIState()
    ui._running["u-boot:do_compile"] = _RunTask(
        pf="u-boot", task="do_compile", start=time.monotonic(), logfile=str(tmp_path / "nope.log")
    )
    assert ui.stall_report() is None


@pytest.mark.unit
def test_stall_report_fresh_log_reports_small_stall(tmp_path) -> None:
    """A just-written log yields a near-zero stall and the task label."""
    log = tmp_path / "log.do_compile"
    log.write_text("compiling\n")
    ui = BuildUIState()
    ui._running["u-boot:do_compile"] = _RunTask(
        pf="u-boot", task="do_compile", start=time.monotonic(), logfile=str(log)
    )
    report = ui.stall_report()
    assert report is not None
    stalled, labels = report
    assert stalled < 5
    assert labels == ["u-boot:do_compile"]


@pytest.mark.unit
def test_stall_report_stale_log_reports_large_stall(tmp_path) -> None:
    """A log untouched for 2h reads back as a ~7200s stall (the u-boot signature)."""
    log = tmp_path / "log.do_compile"
    log.write_text("Project ERROR: GN build error\n")
    old = time.time() - 7200
    os.utime(log, (old, old))
    ui = BuildUIState()
    ui._running["u-boot:do_compile"] = _RunTask(
        pf="u-boot", task="do_compile", start=time.monotonic(), logfile=str(log)
    )
    report = ui.stall_report()
    assert report is not None
    stalled, labels = report
    assert 7150 <= stalled <= 7260
    assert labels == ["u-boot:do_compile"]


@pytest.mark.unit
def test_stall_report_uses_freshest_running_log(tmp_path) -> None:
    """With one stale and one fresh running task, the freshest log wins (build is alive)."""
    stale = tmp_path / "stale.log"
    stale.write_text("x\n")
    old = time.time() - 7200
    os.utime(stale, (old, old))
    fresh = tmp_path / "fresh.log"
    fresh.write_text("y\n")
    ui = BuildUIState()
    ui._running["a:do_compile"] = _RunTask(pf="a", task="do_compile", start=time.monotonic(), logfile=str(stale))
    ui._running["b:do_compile"] = _RunTask(pf="b", task="do_compile", start=time.monotonic(), logfile=str(fresh))
    report = ui.stall_report()
    assert report is not None
    stalled, labels = report
    assert stalled < 5
    assert set(labels) == {"a:do_compile", "b:do_compile"}


# ---------------------------------------------------------------------------
# Regex-fallback: second-invocation re-parse reset
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_regex_reparse_after_build_resets_bar() -> None:
    """In degraded (regex) mode, a Parsing line after a Running-task line marks a
    second bitbake invocation and resets the build bar for the new cycle."""
    ui = BuildUIState()
    # Cycle 1: a running-task line flips to BUILD and fills the bar.
    ui.process_line("NOTE: Running task 1 of 1 (/qtwebengine.bb:do_cleansstate)")
    assert ui._phase is _Phase.BUILD
    assert ui._build_progress.tasks[0].total == 1

    # Cycle 2: the second bitbake re-parses -> reset back to SETUP, count cleared.
    ui.process_line("Parsing recipes:  10% || ETA:  0:00:30")
    assert ui._phase is _Phase.SETUP
    assert ui._build_progress.tasks[0].completed == 0

    # Cycle 2 tasks: the bar tracks the new, larger total (overwriting the stale 1).
    ui.process_line("NOTE: Running task 200 of 9005 (/qtwebengine.bb:do_compile)")
    assert ui._build_progress.tasks[0].total == 9005


@pytest.mark.unit
def test_set_dist_lines_injected_in_building_frame() -> None:
    ui = BuildUIState()
    ui.process_line("NOTE: Running task 1200 of 9005 (/x.bb:do_compile)")
    ui.set_dist_lines([Text("cluster: x"), Text("daemon: y")])

    # No running tasks: Group is [header, cluster, daemon, build_progress].
    inner = ui.make_renderable().renderables
    plains = [r.plain for r in inner if isinstance(r, Text)]
    assert "cluster: x" in plains
    assert "daemon: y" in plains
    assert ui._build_progress in inner
    # The dist lines sit between the header and the build bar.
    bar_index = inner.index(ui._build_progress)
    cluster_index = next(i for i, r in enumerate(inner) if isinstance(r, Text) and r.plain == "cluster: x")
    assert 0 < cluster_index < bar_index


@pytest.mark.unit
def test_set_dist_lines_empty_by_default() -> None:
    ui = BuildUIState()
    ui.process_line("NOTE: Running task 1200 of 9005 (/x.bb:do_compile)")
    # No set_dist_lines call: Group is [header, build_progress] as before.
    inner = ui.make_renderable().renderables
    assert len(inner) == 2
    assert inner[1] is ui._build_progress


@pytest.mark.unit
def test_make_renderable_shows_cache_badge_when_active() -> None:
    ui = BuildUIState()
    ui.process_line("NOTE: Running task 10 of 100 (/x.bb:do_compile)")
    ui.set_cache_badge(active=True, hit_pct=90.0, verdict="DISTRIBUTING")
    inner = ui.make_renderable().renderables
    flat = " ".join(r.plain for r in inner if isinstance(r, Text))
    assert "90%" in flat
    assert "dist on" in flat


@pytest.mark.unit
def test_make_renderable_cache_badge_ccache_has_no_dist() -> None:
    ui = BuildUIState()
    ui.process_line("NOTE: Running task 10 of 100 (/x.bb:do_compile)")
    # A ccache build reports no verdict: cache badge only, no dist badge.
    ui.set_cache_badge(active=True, hit_pct=75.0, verdict=None)
    inner = ui.make_renderable().renderables
    flat = " ".join(r.plain for r in inner if isinstance(r, Text))
    assert "75%" in flat
    assert "dist" not in flat


@pytest.mark.unit
def test_make_renderable_no_cache_badge_by_default() -> None:
    ui = BuildUIState()
    ui.process_line("NOTE: Running task 10 of 100 (/x.bb:do_compile)")
    inner = ui.make_renderable().renderables
    flat = " ".join(r.plain for r in inner if isinstance(r, Text))
    assert "dist" not in flat
    assert len(inner) == 2


# ---------------------------------------------------------------------------
# Cache-backend classification badge — make_renderable / reset-cycle
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_make_renderable_cache_badge_distinct_per_backend() -> None:
    """Each of the four cache_backend states renders a visually distinct badge
    cell (glyph + colour) in the running-task table, not just distinct internal
    state - covers make_renderable() end to end, unlike the process_event-level
    coverage in test_build_ui_events.py."""
    ui = BuildUIState()
    ui.process_line("NOTE: Running task 1200 of 9005 (/x.bb:do_compile)")
    base = time.monotonic()
    ui._running["a:do_compile"] = _RunTask(
        pf="pkg-a-1.0-r0", task="do_compile", start=base - 4, cache_backend="sccache"
    )
    ui._running["b:do_compile"] = _RunTask(pf="pkg-b-1.0-r0", task="do_compile", start=base - 3, cache_backend="ccache")
    ui._running["c:do_compile"] = _RunTask(pf="pkg-c-1.0-r0", task="do_compile", start=base - 2, cache_backend="none")
    ui._running["d:do_compile"] = _RunTask(pf="pkg-d-1.0-r0", task="do_compile", start=base - 1, cache_backend=None)

    table = ui.make_renderable().renderables[-1]
    # Columns: 0=spinner, 1=icon, 2=cache-backend badge, 3=pf, 4=task, 5=elapsed.
    badge_cells = table.columns[2]._cells
    glyphs = [c.plain for c in badge_cells]
    styles = [str(c.style) for c in badge_cells]

    # Rows sort longest-elapsed first: a, b, c, d.
    assert glyphs[0] == cache_render.cache_backend_badge("sccache")[0]
    assert glyphs[1] == cache_render.cache_backend_badge("ccache")[0]
    assert glyphs[2] == cache_render.cache_backend_badge("none")[0]
    assert glyphs[3] == ""  # unclassified (cache_backend=None) renders blank, not a placeholder

    # Four (glyph, style) pairs must all differ - a real visual distinction,
    # not four cells that happen to render the same.
    assert len({(g, s) for g, s in zip(glyphs, styles, strict=True)}) == 4


@pytest.mark.unit
def test_reset_for_new_build_cycle_clears_cache_classification() -> None:
    """A running task carrying a cache_backend classification must not survive
    into the next bitbake invocation's cycle - _running is cleared wholesale,
    same as the other per-cycle fields the existing reset tests assert on."""
    ui = BuildUIState()
    ui._running["glibc-2.39-r0:do_compile"] = _RunTask(
        pf="glibc-2.39-r0", task="do_compile", start=time.monotonic(), cache_backend="sccache"
    )
    assert ui._running  # sanity: populated before the reset

    ui._reset_for_new_build_cycle()

    assert ui._running == {}


@pytest.mark.unit
def test_show_baseline_drift_default_false_keeps_baselines_empty(tmp_path: Path) -> None:
    """Default construction must not load baselines even when a populated
    timings_path is given - show_baseline_drift defaults to False."""
    timings = tmp_path / "t.json"
    timings.write_text(
        json.dumps({"schema_version": 2, "tasks": {"glibc:do_compile": {"count": 1, "mean": 10.0, "m2": 0.0}}}),
        encoding="utf-8",
    )
    ui = BuildUIState(timings_path=timings)
    assert ui._task_baselines == {}


@pytest.mark.unit
def test_show_baseline_drift_true_loads_baselines(tmp_path: Path) -> None:
    """show_baseline_drift=True loads the populated timings_path baselines."""
    timings = tmp_path / "t.json"
    timings.write_text(
        json.dumps({"schema_version": 2, "tasks": {"glibc:do_compile": {"count": 1, "mean": 10.0, "m2": 0.0}}}),
        encoding="utf-8",
    )
    ui = BuildUIState(timings_path=timings, show_baseline_drift=True)
    assert ui._task_baselines == {"glibc:do_compile": (10.0, 0.0)}
