"""Tests that build.py resolves the output mode and threads it into every run context."""

from __future__ import annotations

from pathlib import Path

import bakar.commands.build as build
from bakar.output_mode import OutputMode


class _FakeStderr:
    def __init__(self, tty: bool) -> None:
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def test_output_mode_plain_when_piped(monkeypatch) -> None:
    monkeypatch.setattr(build, "global_output_mode_override", lambda: None)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr("sys.stderr", _FakeStderr(tty=False))
    assert build._output_mode() is OutputMode.PLAIN


def test_output_mode_honors_plain_override_on_tty(monkeypatch) -> None:
    monkeypatch.setattr(build, "global_output_mode_override", lambda: OutputMode.PLAIN)
    monkeypatch.setattr("sys.stderr", _FakeStderr(tty=True))
    assert build._output_mode() is OutputMode.PLAIN


def test_output_mode_rich_override_wins_when_piped(monkeypatch) -> None:
    monkeypatch.setattr(build, "global_output_mode_override", lambda: OutputMode.RICH)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr("sys.stderr", _FakeStderr(tty=False))
    assert build._output_mode() is OutputMode.RICH


def test_plain_render_console_is_no_color(monkeypatch) -> None:
    monkeypatch.setattr(build, "global_output_mode_override", lambda: OutputMode.PLAIN)
    console = build._plain_render_console()
    assert console is not None
    assert console.no_color is True


def test_render_console_none_in_rich(monkeypatch) -> None:
    monkeypatch.setattr(build, "global_output_mode_override", lambda: OutputMode.RICH)
    assert build._plain_render_console() is None


def test_every_build_site_threads_the_mode() -> None:
    # No construction site may be left on the RICH default / shared console.
    src = Path(build.__file__).read_text(encoding="utf-8")
    assert src.count("KasBuildContext(") == src.count("output_mode=_output_mode()")
    assert src.count("RunLogger(runs_dir=cfg.runs_dir") == src.count("render_console=_plain_render_console()")
    assert src.count("KasBuildContext(") == 3
    assert src.count("RunLogger(runs_dir=cfg.runs_dir") == 3
