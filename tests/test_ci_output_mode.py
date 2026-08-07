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


def test_journal_report_carries_typed_progress() -> None:
    """The journal snapshot returns values, not a line, so journalctl can filter on them."""
    ui = BuildUIState(start_monotonic=time.monotonic())
    ui.process_line("Running task 12 of 40")
    report = ui.journal_report()
    assert report["tasks_done"] == 12
    assert report["tasks_total"] == 40
    assert report["pct"] == "30.0"
    assert isinstance(report["elapsed_s"], int)


def test_journal_report_omits_total_before_bitbake_reports_one() -> None:
    """Total is None until bitbake announces it; emit no field rather than a guess."""
    report = BuildUIState(start_monotonic=time.monotonic()).journal_report()
    assert "tasks_total" not in report
    assert "pct" not in report
    assert report["tasks_done"] == 0


def test_journal_report_bounds_the_running_sample() -> None:
    """A wide host can run tens of tasks; a journal record is a single datagram."""
    ui = BuildUIState(start_monotonic=time.monotonic())
    for i in range(12):
        ui.process_line(f"recipe pkg-{i}-1.0: task do_compile: Started")
    report = ui.journal_report()
    assert report["running"] == 12
    assert len(str(report["running_sample"]).split(",")) <= 5


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


def test_plain_controller_exposes_vertical_overflow() -> None:
    """The failure-freeze path is shared with the Rich branch, so the stub needs it.

    _process_line reads live.vertical_overflow before the freeze and writes it
    back after the restart; without the attribute plain mode would AttributeError
    on the first failed task.
    """
    ui = BuildUIState(start_monotonic=time.monotonic())
    console = Console(no_color=True, force_terminal=False, file=StringIO())
    live = _PlainFrameController(ui, console, threading.Event())
    saved = live.vertical_overflow
    live.vertical_overflow = "visible"
    live.vertical_overflow = saved
    assert live.vertical_overflow == "ellipsis"


def test_rich_live_stop_clobbers_vertical_overflow() -> None:
    """Pins the Rich behaviour the freeze/restart restore exists to undo.

    Live.stop() sets vertical_overflow="visible" so its final frame renders
    uncropped, and never restores it. Because kas_build restarts the Live after
    a task failure, every later frame would then render uncropped and Rich's
    cursor-up erase - sized for a panel taller than the terminal - overshoots,
    stacking a fresh panel per refresh instead of redrawing in place.

    If this test ever fails, Rich stopped clobbering the value and the manual
    restore in _process_line can go.
    """
    from rich.live import Live
    from rich.text import Text

    console = Console(force_terminal=True, file=StringIO())
    live = Live(get_renderable=lambda: Text("frame"), console=console, refresh_per_second=8)
    live.start()
    assert live.vertical_overflow == "ellipsis"
    live.stop()
    assert live.vertical_overflow == "visible"


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
