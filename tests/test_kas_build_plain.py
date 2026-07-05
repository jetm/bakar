"""Tests for the plain-mode frame controller in kas_build."""

from __future__ import annotations

import threading
from io import StringIO

from rich.console import Console
from rich.table import Table

from bakar.steps.build_ui import BuildUIState
from bakar.steps.kas_build import _PlainFrameController

_ESC = "\x1b"


def _controller(buf: StringIO) -> _PlainFrameController:
    console = Console(no_color=True, force_terminal=False, file=buf)
    stop = threading.Event()
    stop.set()  # loop exits on the first wait, so the thread joins immediately
    return _PlainFrameController(BuildUIState(start_monotonic=1.0), console, stop)


def test_shim_start_refresh_and_transient_do_not_raise() -> None:
    # R1: the restart path calls live.start(refresh=True) and writes live.transient.
    with _controller(StringIO()) as live:
        live.stop()
        live.start(refresh=True)
        live.transient = True


def test_shim_renders_table_plainly() -> None:
    # The closures pass Rich Table renderables (layer_hash_table); a no-color
    # console must render them to plain text, not repr, and emit no ANSI.
    buf = StringIO()
    with _controller(buf) as live:
        table = Table()
        table.add_column("layer")
        table.add_row("layerhash123")
        live.console.print(table)
    out = buf.getvalue()
    assert _ESC not in out
    assert "layerhash123" in out
    assert "rich.table.Table" not in out


def test_shim_console_prints_plain_status_without_ansi() -> None:
    # The status line has literal brackets ("bakar[build]"); the loop prints with
    # markup=False so Rich does not eat them as a style tag. Mirror that here.
    buf = StringIO()
    with _controller(buf) as live:
        live.console.print("bakar[build] phase=tasks tasks=1/2 running=1 elapsed=0s", markup=False)
    out = buf.getvalue()
    assert _ESC not in out
    assert "bakar[build]" in out
    assert "phase=tasks" in out
