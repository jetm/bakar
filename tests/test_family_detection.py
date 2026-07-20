"""Tests for data-driven manifest-family detection.

``_family_from_workspace_contents`` inspects the workspace tree (rather than the
cwd) so ``bakar monitor -w <ws>`` run from anywhere resolves the right family.
``_bsp_from_cwd`` keeps its cwd-based signal but now also recognizes qcom. The
monitor-level test drives the CLI end to end against a synthetic qcom workspace
and asserts the run under ``<ws>/qcom/build-<distro>/runs`` is found - probes and
the event-log reader are patched so no real sccache/docker/bitbake runs.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

import bakar.commands.monitor as monitor_module
import bakar.eventlog as eventlog_module
from bakar.cli import app
from bakar.commands._helpers import _bsp_from_cwd, _family_from_workspace_contents
from bakar.diagnostics import CcacheReport

if TYPE_CHECKING:
    from pathlib import Path

    from typer.testing import CliRunner as _CliRunner

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# _family_from_workspace_contents
# ---------------------------------------------------------------------------


def test_family_from_contents_qcom_via_repo(tmp_path: Path) -> None:
    """A ``<ws>/qcom/.repo`` dir (repo sync output) identifies the qcom family."""
    (tmp_path / "qcom" / ".repo").mkdir(parents=True)

    assert _family_from_workspace_contents(tmp_path) == "qcom"


def test_family_from_contents_qcom_via_build_dir(tmp_path: Path) -> None:
    """A ``<ws>/qcom/build-<distro>`` dir identifies qcom even without .repo."""
    (tmp_path / "qcom" / "build-qcom-wayland").mkdir(parents=True)

    assert _family_from_workspace_contents(tmp_path) == "qcom"


def test_family_from_contents_nxp_via_repo(tmp_path: Path) -> None:
    """A ``<ws>/nxp/.repo`` dir identifies the nxp family."""
    (tmp_path / "nxp" / ".repo").mkdir(parents=True)

    assert _family_from_workspace_contents(tmp_path) == "nxp"


def test_family_from_contents_empty_workspace_is_none(tmp_path: Path) -> None:
    """A workspace with no family subtree yields None (caller falls back)."""
    assert _family_from_workspace_contents(tmp_path) is None


def test_family_from_contents_ordered_nxp_wins_over_qcom(tmp_path: Path) -> None:
    """The ordered probe returns the first match (nxp before qcom)."""
    (tmp_path / "nxp" / ".repo").mkdir(parents=True)
    (tmp_path / "qcom" / ".repo").mkdir(parents=True)

    assert _family_from_workspace_contents(tmp_path) == "nxp"


# ---------------------------------------------------------------------------
# _bsp_from_cwd qcom case
# ---------------------------------------------------------------------------


def test_bsp_from_cwd_recognizes_qcom(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """cwd inside ``<ws>/qcom/`` resolves to the qcom family."""
    qcom = tmp_path / "qcom" / "layers"
    qcom.mkdir(parents=True)
    monkeypatch.chdir(qcom)

    assert _bsp_from_cwd(tmp_path) == "qcom"


# ---------------------------------------------------------------------------
# monitor resolves a qcom workspace end to end
# ---------------------------------------------------------------------------


def _synthetic_artifact() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "build": {
            "started": None,
            "completed": None,
            "outcome": "unknown",
            "tasks_total": 10,
            "tasks_completed": 4,
            "tasks_active": 0,
        },
        "tasks": [],
        "setscene": {"covered": 0, "notcovered": 0, "total": 0, "per_recipe": []},
        "failures": [],
    }


def test_monitor_resolves_qcom_workspace_run(
    runner: _CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``bakar monitor -w <qcom-ws>`` finds the run under qcom/build-<distro>/runs.

    The workspace carries no cwd signal (the runner stands elsewhere), so the
    data-driven content probe must resolve qcom and ``cfg.runs_dir`` must land
    at ``<ws>/qcom/build-qcom-wayland/runs``.
    """
    run_id = "20260601-120000"
    run = tmp_path / "qcom" / "build-qcom-wayland" / "runs" / run_id
    run.mkdir(parents=True)

    monkeypatch.setenv("BAKAR_SCCACHE_DIST", "0")
    monkeypatch.setattr(monitor_module, "normalize", lambda _path: _synthetic_artifact())
    monkeypatch.setattr(eventlog_module, "normalize", lambda _path: _synthetic_artifact())
    monkeypatch.setattr(monitor_module, "is_build_running", lambda _run_dir: (False, None, False))
    monkeypatch.setattr(
        monitor_module,
        "probe_ccache",
        lambda _dir: CcacheReport(available=False, error="patched: no ccache in test"),
    )

    result = runner.invoke(app, ["monitor", "--json", "--once", "--workspace", str(tmp_path)])

    assert result.exit_code == 0, result.stderr
    doc = json.loads(result.stdout)
    assert doc["run"] == run_id
