"""Tests for bakar's CI/plain output-mode resolution and plain rendering.

Resolver unit tests (task 1.1) live here; the integration behaviors referenced by the
threat model (task 8.1) are appended below the resolver block. The --json mode-invariance
check (formerly duplicated here) now lives solely in test_monitor_plain.py.
"""

from __future__ import annotations

import threading
import time
from io import StringIO

from rich.console import Console

import bakar.cli  # noqa: F401 - registers all subcommands on the shared app
import bakar.steps.kas_build as kas_build
from bakar import eventlog
from bakar.output_mode import OutputMode, resolve_output_mode
from bakar.steps.build_ui import BuildUIState
from bakar.steps.kas_build import _PlainFrameController
from tests.conftest import _GLYPHS

_ESC = "\x1b"


def test_piped_selects_plain() -> None:
    assert resolve_output_mode(None, isatty=False, ci_env=None) is OutputMode.PLAIN


def test_tty_no_ci_stays_rich() -> None:
    assert resolve_output_mode(None, isatty=True, ci_env=None) is OutputMode.RICH


def test_ci_env_selects_plain_on_tty() -> None:
    assert resolve_output_mode(None, isatty=True, ci_env="1") is OutputMode.PLAIN


def test_falsey_ci_env_selects_rich_on_tty() -> None:
    for ci in ("", "0", "false", "False"):
        assert resolve_output_mode(None, isatty=True, ci_env=ci) is OutputMode.RICH


def test_explicit_plain_override_wins_on_tty() -> None:
    assert resolve_output_mode(OutputMode.PLAIN, isatty=True, ci_env=None) is OutputMode.PLAIN


def test_explicit_rich_override_wins_under_ci() -> None:
    assert resolve_output_mode(OutputMode.RICH, isatty=False, ci_env="1") is OutputMode.RICH


# --- Integration behaviors referenced by the threat model (task 8.1) -----------


def test_plain_has_no_ansi(tmp_path) -> None:
    # Drive a plain-mode frame controller with a fed build state (stand-in for the
    # PTY feed) and assert the emitted status carries no ANSI escape and no glyph.
    ui = BuildUIState(start_monotonic=time.monotonic())
    ui.process_line("Running task 12 of 40")
    ui.process_line("recipe foo-1.0: task do_compile: Started")
    buf = StringIO()
    console = Console(no_color=True, force_terminal=False, file=buf)
    stop = threading.Event()
    stop.set()
    with _PlainFrameController(ui, console, stop) as live:
        line = ui.plain_status_line()
        live.console.print(line, markup=False)
    out = buf.getvalue()
    assert _ESC not in out
    assert not any(g in out for g in _GLYPHS)
    assert "tasks=12/40" in out


def test_plain_status_throttles(monkeypatch) -> None:
    # The tick is the throttle: many rapid state changes must emit ~window/interval
    # lines, not one per change.
    monkeypatch.setattr(kas_build, "_PLAIN_STATUS_INTERVAL", 0.02)
    ui = BuildUIState(start_monotonic=time.monotonic())
    buf = StringIO()
    console = Console(no_color=True, force_terminal=False, file=buf)
    stop = threading.Event()
    churn = 0
    with _PlainFrameController(ui, console, stop):
        end = time.monotonic() + 0.2
        while time.monotonic() < end:
            churn += 1
            ui.process_line(f"Running task {churn} of 100000")
            time.sleep(0.001)
        stop.set()
    lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
    # ~0.2s / 0.02s tick -> at most ~10 emissions; generous ceiling for jitter.
    assert len(lines) <= 20
    assert churn > 3 * max(len(lines), 1)


def test_plain_failure_line() -> None:
    # A task failure surfaces a plain recipe:task line with no markup or glyph.
    ui = BuildUIState(start_monotonic=time.monotonic())
    ev = eventlog._EventStub(_package="bar-2.0", _task="do_install")
    ui.process_event("bb.build.TaskFailed", ev)
    alerts = ui.take_pending_alerts()
    assert alerts
    buf = StringIO()
    console = Console(no_color=True, force_terminal=False, file=buf)
    for alert in alerts:
        console.print(alert)
    out = buf.getvalue()
    assert "bar-2.0" in out
    assert "do_install" in out
    assert _ESC not in out
    assert not any(g in out for g in _GLYPHS)


def test_plain_runner_consumes_events() -> None:
    # A4 proxy: BuildUIState consumes the structured feed regardless of render mode,
    # so plain mode retains progress data.
    ui = BuildUIState(start_monotonic=time.monotonic())
    stats = {
        "total": 50,
        "completed": 5,
        "active": 2,
        "setscene_covered": 0,
        "setscene_total": 0,
        "setscene_notcovered": 0,
    }
    ev = eventlog._EventStub(stats=stats)
    ui.process_event("bb.runqueue.runQueueTaskStarted", ev)
    line = ui.plain_status_line()
    assert line is not None
    assert "tasks=7/50" in line
