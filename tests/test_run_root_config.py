"""report/insights derive their config root from the selected run's location.

A preset fan-out run lives in a sub-workspace (``<ws>/build/<preset>/nxp/...``
or ``<setup>/build/<preset>/build/runs``). Layer hashes, buildhistory and the
timing baseline are read off ``cfg.bsp_root``, so it must be the sub-workspace's
root, not the default ``<ws>/nxp`` or the setup dir.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

import bakar.commands.report as report_module
from bakar import task_timings
from bakar.cli import app
from bakar.commands._helpers import resolve_run_config
from bakar.report import ReportSummary

if TYPE_CHECKING:
    from pathlib import Path

    from typer.testing import CliRunner as _CliRunner

pytestmark = pytest.mark.unit

_RUN_ID = "20260527-100000-1"


@pytest.fixture(autouse=True)
def _hermetic_home(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("home")))


def _make_bbsetup(setup: Path) -> None:
    (setup / "config").mkdir(parents=True)
    (setup / "config" / "config-upstream.json").write_text(json.dumps({"data": {}, "bitbake-config": {}}))
    (setup / "build").mkdir(exist_ok=True)
    (setup / "build" / "init-build-env").write_text("")


def _summary() -> ReportSummary:
    return ReportSummary(
        run_id=_RUN_ID,
        status="success",
        duration_s=1.0,
        deploy_dir="/x",
        image_size=1,
        layers=[],
    )


class _StopError(Exception):
    pass


def test_helper_nxp_fan_out(tmp_path: Path) -> None:
    sub = tmp_path / "build" / "rel-a"
    run = sub / "nxp" / "build" / "runs" / _RUN_ID
    run.mkdir(parents=True)
    assert resolve_run_config(run, "nxp").bsp_root == sub / "nxp"


def test_helper_nxp_default_unchanged(tmp_path: Path) -> None:
    run = tmp_path / "nxp" / "build" / "runs" / _RUN_ID
    run.mkdir(parents=True)
    assert resolve_run_config(run, "nxp").bsp_root == tmp_path / "nxp"


def test_helper_bbsetup_fan_out(tmp_path: Path) -> None:
    _make_bbsetup(tmp_path)
    sub = tmp_path / "build" / "rel-a"
    run = sub / "build" / "runs" / _RUN_ID
    run.mkdir(parents=True)
    assert resolve_run_config(run, "bbsetup").bsp_root == sub


def test_report_nxp_fan_out_uses_sub_workspace(
    runner: _CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "nxp").mkdir()
    sub = tmp_path / "build" / "rel-a"
    (sub / "nxp" / "build" / "runs" / _RUN_ID).mkdir(parents=True)
    seen = []

    def fake_assemble(run_dir: Path, cfg):
        seen.append(cfg.bsp_root)
        return _summary()

    monkeypatch.setattr(report_module, "assemble_report", fake_assemble)
    result = runner.invoke(app, ["report", "--workspace", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert seen == [sub / "nxp"]


def test_report_bbsetup_fan_out_uses_sub_workspace(
    runner: _CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_bbsetup(tmp_path)
    sub = tmp_path / "build" / "rel-a"
    (sub / "build" / "runs" / _RUN_ID).mkdir(parents=True)
    seen = []

    def fake_assemble(run_dir: Path, cfg):
        seen.append(cfg.bsp_root)
        return _summary()

    monkeypatch.setattr(report_module, "assemble_report", fake_assemble)
    result = runner.invoke(app, ["report", "--workspace", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert seen == [sub.resolve()]


@pytest.mark.parametrize("kind", ["nxp", "bbsetup"])
def test_insights_baseline_path_uses_sub_workspace(
    kind: str, runner: _CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sub = tmp_path / "build" / "rel-a"
    if kind == "nxp":
        (tmp_path / "nxp").mkdir()
        (sub / "nxp" / "build" / "runs" / _RUN_ID).mkdir(parents=True)
        expected = sub / "nxp"
    else:
        _make_bbsetup(tmp_path)
        (sub / "build" / "runs" / _RUN_ID).mkdir(parents=True)
        expected = sub.resolve()
    seen = []

    def fake_path(bsp_root: Path, *args, **kwargs):
        seen.append(bsp_root)
        raise _StopError

    monkeypatch.setattr(task_timings, "timings_path_for", fake_path)
    result = runner.invoke(app, ["insights", "--workspace", str(tmp_path)])
    assert isinstance(result.exception, _StopError), result.output
    assert seen == [expected]
