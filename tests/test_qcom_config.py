"""Tests for ``bakar.config.resolve`` with the qcom BSP family.

Pins the qcom defaults (machine/distro/image/manifest/branch), the
quic-yocto repo URL default, the ``BAKAR_REPO_URL`` env override, and
the qcom path properties (``bsp_root``, ``manifest_path``,
``workspace_subdir``). Mirrors the NXP resolution tests but exercises
the fixed-branch, no-user-config qcom branch.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from bakar.config import (
    DEFAULT_QCOM_DISTRO,
    DEFAULT_QCOM_IMAGE,
    DEFAULT_QCOM_MACHINE,
    DEFAULT_QCOM_MANIFEST,
    DEFAULT_QCOM_REPO_BRANCH,
    DEFAULT_QCOM_REPO_URL,
    resolve,
)

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


def _workspace(tmp_path: Path) -> Path:
    (tmp_path / "qcom").mkdir(parents=True, exist_ok=True)
    return tmp_path


def test_resolve_qcom_defaults(tmp_path: Path) -> None:
    cfg = resolve(workspace=_workspace(tmp_path), bsp_family="qcom")
    assert cfg.bsp_family == "qcom"
    assert cfg.machine == DEFAULT_QCOM_MACHINE == "exmp-q911"
    assert cfg.distro == DEFAULT_QCOM_DISTRO == "qcom-wayland"
    assert cfg.image == DEFAULT_QCOM_IMAGE == "qcom-multimedia-image"
    assert cfg.manifest == DEFAULT_QCOM_MANIFEST
    assert cfg.repo_branch == DEFAULT_QCOM_REPO_BRANCH == "qcom-linux-scarthgap"


def test_resolve_qcom_repo_url_defaults_to_quic_yocto(tmp_path: Path) -> None:
    cfg = resolve(workspace=_workspace(tmp_path), bsp_family="qcom")
    assert cfg.repo_url == DEFAULT_QCOM_REPO_URL
    assert cfg.repo_url == "https://github.com/quic-yocto/qcom-manifest"


def test_resolve_qcom_repo_url_env_override_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BAKAR_REPO_URL", "https://example.invalid/mine.git")
    cfg = resolve(workspace=_workspace(tmp_path), bsp_family="qcom")
    assert cfg.repo_url == "https://example.invalid/mine.git"


def test_resolve_qcom_bsp_root_is_workspace_qcom(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    cfg = resolve(workspace=ws, bsp_family="qcom")
    assert cfg.bsp_root == ws.resolve() / "qcom"
    assert cfg.workspace_subdir == "qcom"


def test_resolve_qcom_manifest_path_under_repo_manifests(tmp_path: Path) -> None:
    cfg = resolve(workspace=_workspace(tmp_path), bsp_family="qcom")
    assert cfg.manifest_path == cfg.bsp_root / ".repo" / "manifests" / cfg.manifest
    assert cfg.manifest_path.name == DEFAULT_QCOM_MANIFEST


def test_resolve_qcom_bblayers_conf_under_build_distro(tmp_path: Path) -> None:
    """QLI's setup-environment writes BUILDDIR=build-<distro>, not build/."""
    cfg = resolve(workspace=_workspace(tmp_path), bsp_family="qcom")
    assert cfg.bblayers_conf == cfg.bsp_root / "build-qcom-wayland" / "conf" / "bblayers.conf"
    assert str(cfg.bblayers_conf).endswith("build-qcom-wayland/conf/bblayers.conf")


def test_resolve_qcom_build_dir_name_is_build_distro(tmp_path: Path) -> None:
    cfg = resolve(workspace=_workspace(tmp_path), bsp_family="qcom")
    assert cfg.build_dir_name == "build-qcom-wayland"


@pytest.mark.parametrize("family", ["nxp", "ti", "generic"])
def test_resolve_non_qcom_build_dir_name_is_build(tmp_path: Path, family: str) -> None:
    (tmp_path / family).mkdir(exist_ok=True)
    cfg = resolve(workspace=tmp_path, bsp_family=family)  # type: ignore[arg-type]
    assert cfg.build_dir_name == "build"


def test_resolve_nxp_bblayers_conf_still_under_build(tmp_path: Path) -> None:
    """Regression guard: the qcom build-<distro> fix must not touch NXP."""
    (tmp_path / "nxp").mkdir(exist_ok=True)
    cfg = resolve(workspace=tmp_path, bsp_family="nxp")
    assert cfg.bblayers_conf == cfg.bsp_root / "build" / "conf" / "bblayers.conf"
