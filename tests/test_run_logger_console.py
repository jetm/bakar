"""Tests for RunLogger's optional per-instance render console (plain mode support)."""

from __future__ import annotations

from rich.console import Console

import bakar.observability as observability
from bakar.observability import RunLogger


def test_default_console_is_module_singleton(tmp_path) -> None:
    log = RunLogger(runs_dir=tmp_path)
    assert log.console is observability.console


def test_render_console_override_is_used(tmp_path) -> None:
    plain = Console(no_color=True, force_terminal=False, stderr=True)
    log = RunLogger(runs_dir=tmp_path, render_console=plain)
    assert log.console is plain
    assert log.console.no_color is True
