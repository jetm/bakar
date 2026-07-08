"""Tests for ``bakar report`` run-directory resolution.

These drive the real ``_find_run`` (not patched) so the command must scan
the on-disk run roots. Real ``events.jsonl`` files are written under
``tmp_path`` so the resolution and status logic run end to end.
``collect_layer_hashes`` and ``_parse_buildhistory`` are patched on
``bakar.report`` (where ``assemble_report`` looks them up) so no real layer
git state or buildhistory tree is needed.

The bug under test: a meta-avocado / custom-build-dir BYO build writes its
runs to ``ws/build-<stem>/build/runs/``, but ``report`` only scanned
``ws/nxp``, ``ws/ti``, and ``ws/build``. It silently picked a stale generic
run and reported failure. The fix also derives ``cfg.bsp_root`` from the
resolved run dir's ``parents[2]``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from bakar.cli import app
from bakar.user_config import UserConfig

if TYPE_CHECKING:
    from pathlib import Path

    from typer.testing import CliRunner as _CliRunner

    from bakar.config import BuildConfig

pytestmark = pytest.mark.unit

_OK_EVENTS = (
    '{"event": "run_start", "ts": "2026-06-03T09:15:41Z"}\n'
    '{"event": "step_ok", "step": "kas_build", "ts": "2026-06-03T09:18:00Z"}\n'
    '{"event": "run_end", "ts": "2026-06-03T09:18:00Z"}\n'
)
_FAIL_EVENTS = (
    '{"event": "run_start", "ts": "2026-06-02T16:34:11Z"}\n'
    '{"event": "step_fail", "step": "kas_build", "ts": "2026-06-02T16:36:00Z"}\n'
    '{"event": "run_end", "ts": "2026-06-02T16:36:00Z"}\n'
)


def _write_run(runs_root: Path, run_id: str, events: str) -> Path:
    run_dir = runs_root / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "events.jsonl").write_text(events)
    return run_dir


def _two_run_workspace(tmp_path: Path) -> Path:
    """Workspace with a stale generic failed run and a fresh build-dir run."""
    ws = tmp_path / "ws"
    _write_run(ws / "build" / "runs", "20260602-000000", _FAIL_EVENTS)
    _write_run(ws / "build-qemux86-64" / "build" / "runs", "20260603-000000", _OK_EVENTS)
    return ws


def test_report_finds_build_stem_run_over_generic_run(runner: _CliRunner, tmp_path: Path) -> None:
    """The fresh ``build-<stem>`` run wins over the stale ``build/`` run."""
    ws = _two_run_workspace(tmp_path)

    with (
        patch("bakar.commands._app._load_user_config_safe", return_value=UserConfig()),
        patch("bakar.commands._app._get_vendors", return_value=[]),
        patch("bakar.commands.report._bbsetup_workspace", return_value=None),
        patch("bakar.report.collect_layer_hashes", return_value=[]),
        patch("bakar.report._parse_buildhistory", return_value=None),
    ):
        result = runner.invoke(app, ["report", "--workspace", str(ws)])

    assert result.exit_code == 0, result.output
    assert "status: success" in result.output
    assert "20260603-000000" in result.output
    assert "20260602-000000" not in result.output


def test_report_derives_bsp_root_from_run_dir(runner: _CliRunner, tmp_path: Path) -> None:
    """``collect_layer_hashes`` sees a cfg rooted at the run dir's parents[2]."""
    ws = _two_run_workspace(tmp_path)
    captured: dict[str, object] = {}

    def _capture(cfg: BuildConfig) -> list:
        captured["bsp_root"] = cfg.bsp_root
        return []

    with (
        patch("bakar.commands._app._load_user_config_safe", return_value=UserConfig()),
        patch("bakar.commands._app._get_vendors", return_value=[]),
        patch("bakar.commands.report._bbsetup_workspace", return_value=None),
        patch("bakar.report.collect_layer_hashes", side_effect=_capture),
        patch("bakar.report._parse_buildhistory", return_value=None),
    ):
        result = runner.invoke(app, ["report", "--workspace", str(ws)])

    assert result.exit_code == 0, result.output
    assert captured["bsp_root"] == ws / "build-qemux86-64"
