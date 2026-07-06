"""Tests for BuildUIState.plain_status_line (plain build status emitter)."""

from __future__ import annotations

import time

from bakar.steps.build_ui import BuildUIState
from tests.conftest import _GLYPHS

_ESC = "\x1b"


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


def test_plain_status_line_badge_tokens_no_ansi() -> None:
    ui = _building_state()
    ui.set_cache_badge(active=True, hit_pct=90.0, verdict="DISTRIBUTING")
    line = ui.plain_status_line()
    assert line is not None
    assert "cache=90%" in line
    assert "dist=on" in line
    assert _ESC not in line


def test_plain_status_line_no_badge_emits_no_cache_token() -> None:
    line = _building_state().plain_status_line()
    assert line is not None
    assert "cache=" not in line
    assert "dist=" not in line


def test_plain_status_line_preserves_existing_field_order() -> None:
    ui = _building_state()
    ui.set_cache_badge(active=True, hit_pct=75.0, verdict=None)
    line = ui.plain_status_line()
    assert line is not None
    # The existing bakar[build] fields keep their order and precede the badge.
    assert line.index("bakar[build]") < line.index("phase=")
    assert line.index("elapsed=") < line.index("cache=")
    # A ccache build (verdict=None) emits the cache token but no dist token.
    assert "cache=75%" in line
    assert "dist=" not in line
