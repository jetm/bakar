"""Temporary diagnostic: report the console state a failing test saw.

Two tests fold their console output at 80 columns on the CI runner although
``COLUMNS`` is 500. This module only builds a string for an assertion message,
so it changes nothing while those tests pass. Remove it, and its two callers,
once the cause is known.
"""

from __future__ import annotations

import json
import os
import sys

_ENV_NAMES = (
    "COLUMNS",
    "LINES",
    "TERM",
    "FORCE_COLOR",
    "NO_COLOR",
    "TTY_COMPATIBLE",
    "TTY_INTERACTIVE",
    "TERMINAL_WIDTH",
    "JUPYTER_COLUMNS",
    "CI",
    "PYTEST_XDIST_WORKER",
)


def _console_state(console) -> dict[str, object]:
    try:
        return {
            "_width": console._width,
            "_height": console._height,
            "size": tuple(console.size),
            "is_terminal": console.is_terminal,
            "is_dumb_terminal": console.is_dumb_terminal,
            "legacy_windows": console.legacy_windows,
            "is_jupyter": console.is_jupyter,
            "environ_is_os_environ": console._environ is os.environ,
            "file": type(console.file).__name__,
            "stderr_flag": console.stderr,
        }
    except Exception as exc:  # noqa: BLE001 - diagnostic must never mask the assertion
        return {"error": repr(exc)}


def _fd_sizes() -> dict[str, object]:
    sizes: dict[str, object] = {}
    for fd in (0, 1, 2):
        try:
            sizes[str(fd)] = tuple(os.get_terminal_size(fd))
        except OSError as exc:
            sizes[str(fd)] = type(exc).__name__
    return sizes


def console_diag() -> str:
    """One JSON line describing the environment and every module-level console."""
    try:
        from rich.console import Console

        from bakar import build_stop, observability
        from bakar.commands import console
        from bakar.steps import remote_dispatch

        consoles = {
            "commands.console": console,
            "observability.console": observability.console,
            "build_stop.console": build_stop.console,
            "remote_dispatch.console": remote_dispatch.console,
            "fresh Console(stderr=True) built now": Console(stderr=True),
        }
        report = {
            "env": {name: os.environ.get(name) for name in _ENV_NAMES},
            "fd_terminal_sizes": _fd_sizes(),
            "sys_stderr": type(sys.stderr).__name__,
            "sys_stderr_isatty": sys.stderr.isatty(),
            "consoles": {name: _console_state(c) for name, c in consoles.items()},
        }
        return "CONSOLE-DIAG " + json.dumps(report, sort_keys=True, default=repr)
    except Exception as exc:  # noqa: BLE001 - diagnostic must never mask the assertion
        return f"CONSOLE-DIAG failed: {exc!r}"
