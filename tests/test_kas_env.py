"""Unit tests for bspctl.steps.kas_build._ccache_args.

Verifies the workspace-root ccache bind-mount that replaced the dangling
per-BSP ``ccache`` symlinks.  The mount is injected via the ``--runtime-args``
CLI flag rather than ``KAS_RUNTIME_ARGS`` env-var, because ``kas-container``
unconditionally overwrites that variable before its option-parsing loop.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from bspctl.config import BuildConfig
from bspctl.steps.kas_build import _build_env, _ccache_args

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _make_cfg(workspace: Path, bsp_family: str = "nxp", *, host_mode: bool = False) -> BuildConfig:
    return BuildConfig(
        workspace=workspace,
        bsp_family=bsp_family,  # type: ignore[arg-type]
        machine="imx8mp-var-dart",
        distro="fsl-imx-xwayland",
        image="core-image-minimal",
        manifest="imx-6.6.52-2.2.2.xml",
        repo_url="https://example.invalid/repo.git",
        repo_branch="imx-6.6.52-2.2.2",
        container_image="jetm/kas-build-env:5.2-f40",
        host_mode=host_mode,
    )


def test_ccache_args_container_mode_returns_flag(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
    args = _ccache_args(cfg)
    expected_mount = f"-v {tmp_path / 'ccache'}:/work/ccache:rw"
    assert args == ["--runtime-args", expected_mount]


def test_ccache_args_creates_dir(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
    assert not (tmp_path / "ccache").exists()
    _ccache_args(cfg)
    assert (tmp_path / "ccache").is_dir()


def test_ccache_args_host_mode_returns_empty(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path, host_mode=True)
    assert _ccache_args(cfg) == []


def test_ccache_args_shared_for_nxp_and_ti(tmp_path: Path) -> None:
    """NXP and TI get identical mount args pointing at the workspace-root cache."""
    cfg_nxp = _make_cfg(tmp_path, bsp_family="nxp")
    cfg_ti = _make_cfg(tmp_path, bsp_family="ti")
    assert _ccache_args(cfg_nxp) == _ccache_args(cfg_ti)


def test_build_env_kas_work_dir_per_bsp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """KAS_WORK_DIR must scope to the BSP subtree, not the workspace root."""
    cfg_ti = _make_cfg(tmp_path, bsp_family="ti")
    cfg_nxp = _make_cfg(tmp_path, bsp_family="nxp")

    env_ti = _build_env(cfg_ti)
    env_nxp = _build_env(cfg_nxp)

    assert env_ti["KAS_WORK_DIR"].endswith("/ti")
    assert env_nxp["KAS_WORK_DIR"].endswith("/nxp")
