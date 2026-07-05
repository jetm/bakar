"""Tests for bakar monitor's plain output view and --json mode-invariance."""

from __future__ import annotations

from unittest import mock

from typer.testing import CliRunner

import bakar.cli  # noqa: F401 - registers all subcommands on the shared app
from bakar.commands._app import app
from bakar.commands.monitor import _render_plain
from bakar.steps import build_ui

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

_SNAP = {
    "run": "20260101-000000",
    "cluster": {
        "reachable": True,
        "error": None,
        "capacity": {"num_servers": 1, "num_cpus": 8, "in_progress": 0, "servers": []},
    },
    "build_daemon": None,
    "daemons": {
        "hashserv": {"url": "ws://h:8686", "running": True},  # nosemgrep
        "prserv": {"host": "h:8585", "running": False},
    },
    "build": {
        "live": True,
        "outcome": None,
        "elapsed_seconds": 61,
        "tasks_done": 10,
        "tasks_total": 100,
        "tasks_remaining": 90,
        "tasks_running": 3,
        "tasks_failed": 1,
        "tasks_setscene_rerun": 2,
        "running": [{"recipe": "foo", "task": "do_compile"}],
        "failures": [{"recipe": "bar", "task": "do_install"}],
    },
    "kas_errors": [],
}


def test_render_plain_no_ansi_or_glyph() -> None:
    text = "\n".join(_render_plain(_SNAP))
    assert _ESC not in text
    assert not any(g in text for g in _GLYPHS)


def test_render_plain_shows_daemons_and_build() -> None:
    text = "\n".join(_render_plain(_SNAP))
    assert "build: [live]" in text
    assert "10/100 tasks (90 left)" in text
    assert "foo" in text and "do_compile" in text
    assert "hashserv h:8686 (up)" in text


def _invoke(args, tmp_path):
    cfg = mock.Mock(runs_dir=tmp_path)
    with (
        mock.patch("bakar.commands.monitor.resolve", return_value=cfg),
        mock.patch("bakar.commands.monitor._resolve_workspace", return_value=tmp_path),
        mock.patch("bakar.commands.monitor._bsp_from_cwd", return_value="nxp"),
        mock.patch("bakar.commands.monitor._resolve_run_dir", return_value=tmp_path),
        mock.patch("bakar.commands.monitor._daemon_status", return_value={}),
        mock.patch("bakar.commands.monitor._resolve_scheduler_url", return_value=None),
        mock.patch("bakar.commands.monitor._snapshot", return_value=dict(_SNAP)),
        mock.patch("bakar.commands.monitor._recent_kas_errors", return_value=[]),
    ):
        return CliRunner().invoke(app, args)


def test_json_identical_across_modes(tmp_path) -> None:
    out_rich = _invoke(["--rich", "monitor", "--json"], tmp_path)
    out_plain = _invoke(["--plain", "monitor", "--json"], tmp_path)
    assert out_rich.exit_code == 0
    assert out_plain.exit_code == 0
    assert out_rich.stdout == out_plain.stdout


def test_once_plain_has_no_ansi_or_glyph(tmp_path) -> None:
    result = _invoke(["--plain", "monitor", "--once"], tmp_path)
    assert result.exit_code == 0
    assert _ESC not in result.output
    assert not any(g in result.output for g in _GLYPHS)
    assert "build: [live]" in result.output
