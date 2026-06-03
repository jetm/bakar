"""Tests for the bbsetup ``layers/`` resolution in collect_layer_hashes.

Unlike test_layer_hashes.py (which mocks git), these tests create real git
repos under a bbsetup workspace's ``layers/`` dir and assert the collector
resolves a non-empty result with a real short hash per layer. This validates
design assumption A1: the explicit ``cfg.bsp_root/layers/<repo>`` strategy
yields a non-empty table for a synced bitbake-setup workspace.

Also covers the sub-app conversion of ``bakar layers`` (task 5.1):
- bare ``bakar layers`` still prints the git short-hash listing
- ``bakar layers inspect`` is recognized as a sub-verb
- ``bakar layers status`` is recognized as a sub-verb
"""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

import bakar.commands.layers  # noqa: F401 - registers sub-app on app
from bakar.cli import app
from bakar.config import resolve
from bakar.layers import collect_layer_hashes

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit

runner = CliRunner()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git_repo(path: Path) -> None:
    """Create a git repo at ``path`` with a single commit."""
    path.mkdir(parents=True, exist_ok=True)
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True, env={**env})
    (path / "README").write_text("x\n")
    subprocess.run(["git", "-C", str(path), "add", "README"], check=True, env={**env})
    subprocess.run(
        ["git", "-C", str(path), "commit", "-q", "-m", "init"],
        check=True,
        env={**env},
    )


def _write_bbsetup_bblayers(cfg, repos: list[str]) -> None:
    """Write a bblayers.conf using the ${TOPDIR}/../layers/<repo> layout."""
    conf = cfg.bblayers_conf
    conf.parent.mkdir(parents=True, exist_ok=True)
    lines = ['BBLAYERS ?= " \\']
    lines.extend(f"  ${{TOPDIR}}/../layers/{repo}/meta-{repo} \\" for repo in repos)
    lines.append('"')
    conf.write_text("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# Original bbsetup collect_layer_hashes tests (preserved unchanged)
# ---------------------------------------------------------------------------


def test_bbsetup_layers_resolve_non_empty(tmp_path: Path) -> None:
    """A bbsetup workspace with layers/<repo> git repos yields a real table."""
    cfg = resolve(workspace=tmp_path, bsp_family="bbsetup")
    repos = ["poky", "meta-openembedded"]
    _write_bbsetup_bblayers(cfg, repos)
    for repo in repos:
        _git_repo(cfg.bsp_root / "layers" / repo)

    result = collect_layer_hashes(cfg)

    resolved = {lh.repo: lh for lh in result if lh.repo in repos}
    assert set(resolved) == set(repos)
    for lh in resolved.values():
        assert lh.short_hash
        assert len(lh.short_hash) >= 7


def test_bbsetup_missing_layer_dir_omitted(tmp_path: Path) -> None:
    """A repo named in bblayers.conf with no layers/<repo> dir is skipped."""
    cfg = resolve(workspace=tmp_path, bsp_family="bbsetup")
    _write_bbsetup_bblayers(cfg, ["poky", "ghost"])
    _git_repo(cfg.bsp_root / "layers" / "poky")  # ghost dir intentionally absent

    result = collect_layer_hashes(cfg)

    assert "poky" in {lh.repo for lh in result}
    assert "ghost" not in {lh.repo for lh in result}


def test_no_bblayers_returns_empty(tmp_path: Path) -> None:
    """A workspace with no bblayers.conf returns [] without raising."""
    cfg = resolve(workspace=tmp_path, bsp_family="bbsetup")
    assert not cfg.bblayers_conf.is_file()

    assert collect_layer_hashes(cfg) == []


# ---------------------------------------------------------------------------
# Sub-app registration tests (task 5.1)
# ---------------------------------------------------------------------------


def test_layers_is_registered_command() -> None:
    """``bakar layers`` appears in the top-level --help."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "layers" in result.output


def test_layers_inspect_is_recognized_subverb() -> None:
    """``bakar layers inspect --help`` exits 0 and is not 'no such command'."""
    result = runner.invoke(app, ["layers", "inspect", "--help"])
    assert result.exit_code == 0
    assert "inspect" in result.output.lower() or "per-layer" in result.output.lower()


def test_layers_status_is_recognized_subverb() -> None:
    """``bakar layers status --help`` exits 0 and is not 'no such command'."""
    result = runner.invoke(app, ["layers", "status", "--help"])
    assert result.exit_code == 0
    assert "status" in result.output.lower() or "MACHINE" in result.output or "summary" in result.output.lower()


# ---------------------------------------------------------------------------
# Bare ``bakar layers`` preserved behaviour
# ---------------------------------------------------------------------------


def test_bare_layers_prints_hash_listing(tmp_path: Path) -> None:
    """Bare ``bakar layers`` still prints the git short-hash + branch listing."""
    fake_hash = [
        type("LH", (), {"repo": "poky", "short_hash": "abc1234", "branch": "main", "version": None})()
    ]

    with (
        patch("bakar.commands.layers._dispatch_bsp", return_value=("nxp", MagicMock())),
        patch("bakar.commands.layers._resolve_workspace", return_value=tmp_path),
        patch("bakar.commands.layers.resolve", return_value=MagicMock(
            bblayers_conf=tmp_path / "build" / "conf" / "bblayers.conf",
            kas_yaml=None,
        )),
        patch("bakar.commands.layers.collect_layer_hashes", return_value=fake_hash),
        patch("bakar.commands.layers._print_layer_hashes") as mock_print,
    ):
        result = runner.invoke(app, ["layers"])

    assert result.exit_code == 0
    mock_print.assert_called_once()


def test_bare_layers_no_hashes_prints_hint(tmp_path: Path) -> None:
    """Bare ``bakar layers`` prints a hint when no layers exist yet."""
    with (
        patch("bakar.commands.layers._dispatch_bsp", return_value=("nxp", MagicMock())),
        patch("bakar.commands.layers._resolve_workspace", return_value=tmp_path),
        patch("bakar.commands.layers.resolve", return_value=MagicMock(
            bblayers_conf=tmp_path / "build" / "conf" / "bblayers.conf",
            kas_yaml=None,
        )),
        patch("bakar.commands.layers.collect_layer_hashes", return_value=[]),
    ):
        result = runner.invoke(app, ["layers"])

    assert result.exit_code == 0
    assert "no layers" in result.output


def test_bare_layers_unknown_subverb_exits_nonzero() -> None:
    """Passing an unknown sub-verb to layers exits non-zero."""
    result = runner.invoke(app, ["layers", "nonexistent-subverb"])
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# ``bakar layers inspect`` sub-command
# ---------------------------------------------------------------------------


def _make_cfg_mock(tmp_path: Path):
    """Return a minimal BuildConfig mock suitable for inspect/status tests."""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    mock_cfg = MagicMock()
    mock_cfg.bblayers_conf = tmp_path / "build" / "conf" / "bblayers.conf"
    mock_cfg.bsp_root = tmp_path
    mock_cfg.kas_yaml = None
    mock_cfg.runs_dir = runs_dir
    return mock_cfg


def test_layers_inspect_text_output(tmp_path: Path) -> None:
    """``bakar layers inspect`` prints per-layer name/priority/compat/version."""
    mock_cfg = _make_cfg_mock(tmp_path)

    show_layers_output = (
        "layer                 path                                      priority\n"
        "========================================================================\n"
        "meta                  /work/sources/poky/meta                   5\n"
        "meta-poky             /work/sources/poky/meta-poky              5\n"
    )

    def fake_run_shell_capture(kas_ctx, command, capture_path, **kwargs):
        capture_path.parent.mkdir(parents=True, exist_ok=True)
        capture_path.write_text(show_layers_output)
        return 0

    with (
        patch("bakar.commands.layers._common_options", return_value=("nxp", MagicMock(), tmp_path, mock_cfg)),
        patch("bakar.commands.layers._overlay_for", return_value=MagicMock()),
        patch("bakar.commands.layers._collect_bblayer_paths", return_value=[]),
        patch("bakar.commands.layers.RunLogger") as mock_rl,
        patch("bakar.commands.layers.KasBuildContext"),
        patch("bakar.commands.layers.step_kas.run_shell_capture", side_effect=fake_run_shell_capture),
    ):
        mock_rl.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_rl.return_value.__exit__ = MagicMock(return_value=False)
        result = runner.invoke(app, ["layers", "inspect"])

    assert result.exit_code == 0
    # The merged show-layers data should be in the output
    assert "meta" in result.output


def test_layers_inspect_json_output(tmp_path: Path) -> None:
    """``bakar layers inspect --json`` emits a parseable JSON list."""
    mock_cfg = _make_cfg_mock(tmp_path)

    show_layers_output = (
        "layer                 path                                      priority\n"
        "========================================================================\n"
        "meta                  /work/sources/poky/meta                   5\n"
    )

    def fake_run_shell_capture(kas_ctx, command, capture_path, **kwargs):
        capture_path.parent.mkdir(parents=True, exist_ok=True)
        capture_path.write_text(show_layers_output)
        return 0

    with (
        patch("bakar.commands.layers._common_options", return_value=("nxp", MagicMock(), tmp_path, mock_cfg)),
        patch("bakar.commands.layers._overlay_for", return_value=MagicMock()),
        patch("bakar.commands.layers._collect_bblayer_paths", return_value=[]),
        patch("bakar.commands.layers.RunLogger") as mock_rl,
        patch("bakar.commands.layers.KasBuildContext"),
        patch("bakar.commands.layers.step_kas.run_shell_capture", side_effect=fake_run_shell_capture),
    ):
        mock_rl.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_rl.return_value.__exit__ = MagicMock(return_value=False)
        result = runner.invoke(app, ["layers", "inspect", "--json"])

    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert isinstance(parsed, list)
    # The show-layers output adds "meta" entry
    names = {r["name"] for r in parsed}
    assert "meta" in names


def test_layers_inspect_local_layer_conf(tmp_path: Path) -> None:
    """``bakar layers inspect`` reads priority/compat/version from layer.conf."""
    # Create a layer.conf under a layers/<repo>/meta-<repo>/conf/ path
    layer_dir = tmp_path / "layers" / "poky" / "meta"
    conf_dir = layer_dir / "conf"
    conf_dir.mkdir(parents=True)
    (conf_dir / "layer.conf").write_text(
        'BBFILE_PRIORITY_meta = "5"\n'
        'LAYERSERIES_COMPAT_meta = "scarthgap"\n'
        'LAYERVERSION_meta = "16"\n'
    )

    mock_cfg = _make_cfg_mock(tmp_path)

    def fake_run_shell_capture(kas_ctx, command, capture_path, **kwargs):
        capture_path.parent.mkdir(parents=True, exist_ok=True)
        capture_path.write_text("")
        return 1  # container not available - local data only

    with (
        patch("bakar.commands.layers._common_options", return_value=("bbsetup", None, tmp_path, mock_cfg)),
        patch("bakar.commands.layers._overlay_for", return_value=MagicMock()),
        patch("bakar.commands.layers._collect_bblayer_paths", return_value=[("meta", layer_dir)]),
        patch("bakar.commands.layers.RunLogger") as mock_rl,
        patch("bakar.commands.layers.KasBuildContext"),
        patch("bakar.commands.layers.step_kas.run_shell_capture", side_effect=fake_run_shell_capture),
    ):
        mock_rl.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_rl.return_value.__exit__ = MagicMock(return_value=False)
        result = runner.invoke(app, ["layers", "inspect"])

    assert result.exit_code == 0
    assert "meta" in result.output
    assert "5" in result.output  # priority
    assert "scarthgap" in result.output  # compat
    assert "16" in result.output  # version


# ---------------------------------------------------------------------------
# ``bakar layers status`` sub-command
# ---------------------------------------------------------------------------


def test_layers_status_text_output(tmp_path: Path) -> None:
    """``bakar layers status`` prints MACHINE and DISTRO."""
    mock_cfg = _make_cfg_mock(tmp_path)

    call_count = [0]

    def fake_run_shell_capture(kas_ctx, command, capture_path, **kwargs):
        capture_path.parent.mkdir(parents=True, exist_ok=True)
        var = command.split()[-1]
        values = {
            "MACHINE": 'MACHINE="imx8mp-lpddr4-evk"',
            "DISTRO": 'DISTRO="fsl-imx-xwayland"',
            "DISTRO_CODENAME": 'DISTRO_CODENAME="scarthgap"',
            "BB_NUMBER_THREADS": 'BB_NUMBER_THREADS="16"',
            "PARALLEL_MAKE": 'PARALLEL_MAKE="-j16"',
            "SOURCE_MIRROR_URL": 'SOURCE_MIRROR_URL=""',
            "SSTATE_MIRRORS": 'SSTATE_MIRRORS=""',
            "BB_HASHSERV": 'BB_HASHSERV=""',
        }
        capture_path.write_text(values.get(var, ""))
        call_count[0] += 1
        return 0

    with (
        patch("bakar.commands.layers._common_options", return_value=("nxp", MagicMock(), tmp_path, mock_cfg)),
        patch("bakar.commands.layers._overlay_for", return_value=MagicMock()),
        patch("bakar.commands.layers.RunLogger") as mock_rl,
        patch("bakar.commands.layers.KasBuildContext"),
        patch("bakar.commands.layers.step_kas.run_shell_capture", side_effect=fake_run_shell_capture),
    ):
        mock_rl.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_rl.return_value.__exit__ = MagicMock(return_value=False)
        result = runner.invoke(app, ["layers", "status"])

    assert result.exit_code == 0
    assert "MACHINE" in result.output
    assert "imx8mp-lpddr4-evk" in result.output
    assert "DISTRO" in result.output
    assert "fsl-imx-xwayland" in result.output


def test_layers_status_json_output(tmp_path: Path) -> None:
    """``bakar layers status --json`` emits a parseable JSON object with required keys."""
    mock_cfg = _make_cfg_mock(tmp_path)

    def fake_run_shell_capture(kas_ctx, command, capture_path, **kwargs):
        capture_path.parent.mkdir(parents=True, exist_ok=True)
        var = command.split()[-1]
        values = {
            "MACHINE": 'MACHINE="imx8mp-lpddr4-evk"',
            "DISTRO": 'DISTRO="fsl-imx-xwayland"',
            "DISTRO_CODENAME": 'DISTRO_CODENAME="scarthgap"',
            "BB_NUMBER_THREADS": 'BB_NUMBER_THREADS="16"',
            "PARALLEL_MAKE": 'PARALLEL_MAKE="-j16"',
            "SOURCE_MIRROR_URL": 'SOURCE_MIRROR_URL=""',
            "SSTATE_MIRRORS": 'SSTATE_MIRRORS="file:///sstate"',
            "BB_HASHSERV": 'BB_HASHSERV="http://hashserv:8686"',
        }
        capture_path.write_text(values.get(var, ""))
        return 0

    with (
        patch("bakar.commands.layers._common_options", return_value=("nxp", MagicMock(), tmp_path, mock_cfg)),
        patch("bakar.commands.layers._overlay_for", return_value=MagicMock()),
        patch("bakar.commands.layers.RunLogger") as mock_rl,
        patch("bakar.commands.layers.KasBuildContext"),
        patch("bakar.commands.layers.step_kas.run_shell_capture", side_effect=fake_run_shell_capture),
    ):
        mock_rl.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_rl.return_value.__exit__ = MagicMock(return_value=False)
        result = runner.invoke(app, ["layers", "status", "--json"])

    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert "machine" in parsed
    assert "distro" in parsed
    assert parsed["machine"] == "imx8mp-lpddr4-evk"
    assert parsed["distro"] == "fsl-imx-xwayland"
    assert parsed["sstate_mirrors_configured"] is True
    assert parsed["hashserv_url"] == "http://hashserv:8686"


def test_layers_status_omits_unset_optional_fields(tmp_path: Path) -> None:
    """``bakar layers status`` omits DISTRO_CODENAME line when empty."""
    mock_cfg = _make_cfg_mock(tmp_path)

    def fake_run_shell_capture(kas_ctx, command, capture_path, **kwargs):
        capture_path.parent.mkdir(parents=True, exist_ok=True)
        var = command.split()[-1]
        values = {
            "MACHINE": 'MACHINE="some-machine"',
            "DISTRO": 'DISTRO="some-distro"',
            "DISTRO_CODENAME": 'DISTRO_CODENAME=""',
            "BB_NUMBER_THREADS": 'BB_NUMBER_THREADS=""',
            "PARALLEL_MAKE": 'PARALLEL_MAKE=""',
            "SOURCE_MIRROR_URL": 'SOURCE_MIRROR_URL=""',
            "SSTATE_MIRRORS": 'SSTATE_MIRRORS=""',
            "BB_HASHSERV": 'BB_HASHSERV=""',
        }
        capture_path.write_text(values.get(var, ""))
        return 0

    with (
        patch("bakar.commands.layers._common_options", return_value=("nxp", MagicMock(), tmp_path, mock_cfg)),
        patch("bakar.commands.layers._overlay_for", return_value=MagicMock()),
        patch("bakar.commands.layers.RunLogger") as mock_rl,
        patch("bakar.commands.layers.KasBuildContext"),
        patch("bakar.commands.layers.step_kas.run_shell_capture", side_effect=fake_run_shell_capture),
    ):
        mock_rl.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_rl.return_value.__exit__ = MagicMock(return_value=False)
        result = runner.invoke(app, ["layers", "status"])

    assert result.exit_code == 0
    assert "MACHINE" in result.output
    assert "some-machine" in result.output
    assert "not configured" in result.output  # hashserv line
