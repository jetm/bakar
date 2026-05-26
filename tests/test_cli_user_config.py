"""Tests for user-config-driven behavior of the ``bspctl build`` command.

Exercises the doctor auto-run gate and the ``--show-layers`` / ``show_hashes``
layer-table gate through the Typer ``CliRunner`` with ``--dry-run`` so no real
kas-container invocation, sync, or git work happens. The pieces that would
touch the real workspace (``run_all``, ``collect_layer_hashes``, the sync /
setup / gen-kas steps) are monkeypatched.

Follows the CliRunner invocation style in ``tests/test_cli_build_yaml.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

import bspctl.commands._app as app_module
import bspctl.commands._helpers as helpers_module
import bspctl.commands.build as build_module
from bspctl.cli import app
from bspctl.layers import LayerHash
from bspctl.user_config import UserConfig
from bspctl.workspace import WorkspaceState

if TYPE_CHECKING:
    from pathlib import Path

    from typer.testing import CliRunner as _CliRunner

pytestmark = pytest.mark.unit


def _synced_state() -> WorkspaceState:
    """A workspace state that needs neither sync nor setup-env.

    ``repo_initialized`` + ``sources_populated`` + ``bblayers_present`` with no
    manifest/branch mismatch and no SHA drift yields ``needs_repo_sync`` False
    and ``needs_setup_env`` False, so ``build`` skips both steps and reaches the
    ``--dry-run`` early return without invoking the model's sync/setup methods.
    """
    return WorkspaceState(
        bsp_family="nxp",
        repo_initialized=True,
        sources_populated=True,
        build_dir_exists=False,
        bblayers_present=True,
        kas_yaml_present=True,
        forks_linux_imx=False,
        cache_dirs_ok=True,
        # Match the requested manifest/branch so neither repo_broken nor a
        # manifest/branch mismatch fires; that keeps needs_full_reinit (and
        # therefore needs_setup_env) False on this populated workspace.
        repo_manifest_include="imx-6.12.49-2.2.0.xml",
        repo_manifests_branch="walnascar",
        requested_manifest="imx-6.12.49-2.2.0.xml",
        requested_branch="walnascar",
        sha_drift=(),
    )


@pytest.fixture
def runner() -> _CliRunner:
    from typer.testing import CliRunner

    return CliRunner()


@pytest.fixture
def nxp_workspace(tmp_path: Path) -> Path:
    """A workspace with an ``nxp/`` subdir so workspace detection picks nxp."""
    (tmp_path / "nxp").mkdir()
    return tmp_path


@pytest.fixture(autouse=True)
def _stub_build_steps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralize every step that would touch the real workspace.

    ``run_all`` returns no checks; ``detect`` reports a fully-synced
    workspace so the sync/setup steps are skipped; the override and gen-kas
    steps become no-ops. ``collect_layer_hashes`` is left to individual tests
    to override (default: no layers).
    """
    monkeypatch.setattr(build_module, "run_all", lambda cfg, bsp: [])
    monkeypatch.setattr(build_module, "detect", lambda cfg: _synced_state())
    monkeypatch.setattr(build_module.step_override, "apply", lambda cfg, log=None, **kw: None)
    monkeypatch.setattr(build_module.step_kas, "regenerate_yaml", lambda cfg, log, *, bsp: None)
    monkeypatch.setattr(helpers_module, "collect_layer_hashes", lambda cfg: [])
    # Reset cached vendors so the _main callback does not short-circuit on a
    # stale value from another test.
    app_module._VENDORS = None


def _set_user_config(monkeypatch: pytest.MonkeyPatch, uc: UserConfig) -> None:
    """Make the _main callback load the given UserConfig on every invocation."""
    monkeypatch.setattr(app_module, "load_user_config", lambda *a, **k: uc)


def _invoke_build(runner: _CliRunner, workspace: Path, *extra: str):
    return runner.invoke(
        app,
        ["build", "--dry-run", "--skip-sync", "--workspace", str(workspace), *extra],
    )


# ---------------------------------------------------------------------------
# doctor auto-run gate
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_config_doctor_false_skips_doctor(
    runner: _CliRunner, nxp_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``[build] doctor = false`` suppresses the doctor pre-flight."""
    _set_user_config(monkeypatch, UserConfig(doctor=False))
    result = _invoke_build(runner, nxp_workspace)
    assert result.exit_code == 0, result.output
    assert "doctor:" not in result.output


@pytest.mark.unit
def test_skip_doctor_flag_overrides_config_doctor_true(
    runner: _CliRunner, nxp_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--skip-doctor`` skips the pre-flight even when config keeps it on."""
    _set_user_config(monkeypatch, UserConfig(doctor=True))
    result = _invoke_build(runner, nxp_workspace, "--skip-doctor")
    assert result.exit_code == 0, result.output
    assert "doctor:" not in result.output


@pytest.mark.unit
def test_config_doctor_true_runs_doctor(
    runner: _CliRunner, nxp_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With ``doctor`` on and no ``--skip-doctor``, the pre-flight runs."""
    _set_user_config(monkeypatch, UserConfig(doctor=True))
    result = _invoke_build(runner, nxp_workspace)
    assert result.exit_code == 0, result.output
    # _print_diagnosis([]) prints "doctor: 0/0 checks passed".
    assert "doctor:" in result.output


# ---------------------------------------------------------------------------
# layer-hash table gate
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_show_layers_flag_prints_table(
    runner: _CliRunner, nxp_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--show-layers`` renders the ``layers:`` table from the sentinel."""
    _set_user_config(monkeypatch, UserConfig())
    sentinel = [LayerHash(repo="poky", short_hash="deadbee", branch="scarthgap")]
    monkeypatch.setattr(helpers_module, "collect_layer_hashes", lambda cfg: sentinel)
    result = _invoke_build(runner, nxp_workspace, "--show-layers")
    assert result.exit_code == 0, result.output
    assert "layers:" in result.output
    assert "poky" in result.output


@pytest.mark.unit
def test_config_show_hashes_prints_table_without_flag(
    runner: _CliRunner, nxp_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``[layers] show_hashes = true`` prints the table with no flag passed."""
    _set_user_config(monkeypatch, UserConfig(show_hashes=True))
    sentinel = [LayerHash(repo="meta-imx", short_hash="7890abc", branch="lf-6.12.y")]
    monkeypatch.setattr(helpers_module, "collect_layer_hashes", lambda cfg: sentinel)
    result = _invoke_build(runner, nxp_workspace)
    assert result.exit_code == 0, result.output
    assert "layers:" in result.output
    assert "meta-imx" in result.output


@pytest.mark.unit
def test_no_flag_no_config_omits_layers_table(
    runner: _CliRunner, nxp_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Neither the flag nor the config key: no ``layers:`` table is printed."""
    _set_user_config(monkeypatch, UserConfig())
    sentinel = [LayerHash(repo="poky", short_hash="deadbee", branch="scarthgap")]
    monkeypatch.setattr(helpers_module, "collect_layer_hashes", lambda cfg: sentinel)
    result = _invoke_build(runner, nxp_workspace)
    assert result.exit_code == 0, result.output
    assert "layers:" not in result.output
