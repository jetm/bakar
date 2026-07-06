"""Tests for bakar monitor's plain output view and --json mode-invariance."""

from __future__ import annotations

from bakar.commands.monitor import _render_plain
from tests.conftest import _GLYPHS, MONITOR_SNAPSHOT, _invoke_monitor

_ESC = "\x1b"


def test_render_plain_no_ansi_or_glyph() -> None:
    text = "\n".join(_render_plain(MONITOR_SNAPSHOT))
    assert _ESC not in text
    assert not any(g in text for g in _GLYPHS)


def test_render_plain_shows_daemons_and_build() -> None:
    text = "\n".join(_render_plain(MONITOR_SNAPSHOT))
    assert "build: [live]" in text
    assert "10/100 tasks (90 left)" in text
    assert "foo" in text and "do_compile" in text
    assert "hashserv h:8686 (up)" in text


def test_json_identical_across_modes(tmp_path) -> None:
    out_rich = _invoke_monitor(["--rich", "monitor", "--json"], tmp_path)
    out_plain = _invoke_monitor(["--plain", "monitor", "--json"], tmp_path)
    assert out_rich.exit_code == 0
    assert out_plain.exit_code == 0
    assert out_rich.stdout == out_plain.stdout


def test_once_plain_has_no_ansi_or_glyph(tmp_path) -> None:
    result = _invoke_monitor(["--plain", "monitor", "--once"], tmp_path)
    assert result.exit_code == 0
    assert _ESC not in result.output
    assert not any(g in result.output for g in _GLYPHS)
    assert "build: [live]" in result.output
