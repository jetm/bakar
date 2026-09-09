"""Tests that build.py resolves the output mode and threads it into every run context."""

from __future__ import annotations

from pathlib import Path

import bakar.commands._build_flavors as flavors
import bakar.commands._build_options as build_options
import bakar.commands._post_build as post_build
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
    # The two factories stay on build.py next to the console and RunLogger the
    # other tests here patch, but the flavor dispatchers that call them live in
    # _build_flavors, so the counts only close when both sources are read. A
    # single-module count would silently drop the three dispatcher call sites -
    # exactly the sites this test exists to hold.
    #
    # Read ALL FOUR modules carved out of the original build.py, not just the two
    # that construct anything today. _post_build already holds a KasBuildContext
    # field and drives a second bitbake invocation, which makes it the likeliest
    # home for a future construction; scoped to two files, one added there would
    # default to RICH and the shared console with these counts unchanged. The two
    # extra sources contribute zero, so the numbers below are unaffected.
    src = "".join(Path(mod.__file__).read_text(encoding="utf-8") for mod in (build, flavors, post_build, build_options))
    # Exactly one KasBuildContext(/RunLogger(runs_dir=cfg.runs_dir construction may
    # exist across both modules: the one inside the factory below. A stray ad hoc
    # construction added outside the factories bumps these counts and fails here.
    assert src.count("KasBuildContext(") == 1
    assert src.count("RunLogger(runs_dir=cfg.runs_dir") == 1
    # 1 factory definition + 3 call sites each.
    assert src.count("_make_kas_ctx(") == 4
    assert src.count("_open_run_logger(") == 4
