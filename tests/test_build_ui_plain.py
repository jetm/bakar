"""Tests for BuildUIState.plain_status_line (plain build status emitter)."""

from __future__ import annotations

import time

from bakar.steps import build_ui
from bakar.steps.build_ui import BuildUIState

_ESC = "\x1b"
_GLYPHS = (
    build_ui._ICON_COMPILE,
    build_ui._ICON_FETCH,
    build_ui._ICON_CONFIGURE,
    build_ui._ICON_PACKAGE,
    build_ui._ICON_SETSCENE,
    build_ui._ICON_TIMER,
    build_ui._ICON_DRIFT,
)


def _building_state() -> BuildUIState:
    ui = BuildUIState(start_monotonic=time.monotonic())
    ui.process_line("Running task 142 of 1873")
    ui.process_line("recipe foo-1.0: task do_compile: Started")
    return ui


def test_plain_line_has_no_ansi_or_glyph() -> None:
    line = _building_state().plain_status_line()
    assert line is not None
    assert _ESC not in line
    assert not any(g in line for g in _GLYPHS)


def test_plain_line_reports_counts() -> None:
    line = _building_state().plain_status_line()
    assert line is not None
    assert "tasks=142/1873" in line
    assert "running=1" in line


def test_plain_status_dedup() -> None:
    ui = _building_state()
    first = ui.plain_status_line()
    assert first is not None
    # Unchanged state -> deduped to None.
    assert ui.plain_status_line() is None
    # A real state change produces a fresh line.
    ui.process_line("Running task 200 of 1873")
    changed = ui.plain_status_line()
    assert changed is not None
    assert "tasks=200/1873" in changed


def test_plain_pre_total_renders_question_mark() -> None:
    ui = BuildUIState(start_monotonic=time.monotonic())
    line = ui.plain_status_line()
    assert line is not None
    assert "tasks=0/?" in line
