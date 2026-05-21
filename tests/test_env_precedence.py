"""Tests for env var precedence rules (spec: env-var-namespace).

Verifies the two-level stack in :func:`bspctl.config.resolve`:

1. CLI flag (explicit arg) beats env var.
2. Env var beats BSP-family default.
"""

from __future__ import annotations

from bspctl.config import (
    DEFAULT_NXP_MACHINE,
    DEFAULT_NXP_MANIFEST,
    resolve,
)

_MACHINE_VAR = "BSPCTL_MACHINE"
_MANIFEST_VAR = "BSPCTL_MANIFEST"
_DISTRO_VAR = "BSPCTL_DISTRO"
_IMAGE_VAR = "BSPCTL_IMAGE"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _workspace(tmp_path):
    """Return a workspace path with the nxp subdir present."""
    (tmp_path / "nxp").mkdir(parents=True, exist_ok=True)
    return tmp_path


# ---------------------------------------------------------------------------
# 1. CLI flag beats env var
# ---------------------------------------------------------------------------


def test_cli_machine_beats_env(tmp_path, monkeypatch):
    """Explicit machine arg must win over the active machine env var."""
    monkeypatch.setenv(_MACHINE_VAR, "env-board")

    cfg = resolve(
        workspace=_workspace(tmp_path),
        bsp_family="nxp",
        machine="my-board",  # CLI flag
    )

    assert cfg.machine == "my-board", f"CLI flag 'machine' must override {_MACHINE_VAR}"


def test_cli_manifest_beats_env(tmp_path, monkeypatch):
    """Explicit manifest arg must win over the active manifest env var."""
    monkeypatch.setenv(_MANIFEST_VAR, "imx-6.12.49-2.2.0.xml")

    cfg = resolve(
        workspace=_workspace(tmp_path),
        bsp_family="nxp",
        manifest="imx-6.6.52-2.2.2.xml",  # CLI flag
    )

    assert cfg.manifest == "imx-6.6.52-2.2.2.xml", f"CLI flag 'manifest' must override {_MANIFEST_VAR}"


def test_cli_distro_beats_env(tmp_path, monkeypatch):
    """Explicit distro arg must win over the active distro env var."""
    monkeypatch.setenv(_DISTRO_VAR, "fsl-imx-wayland")

    cfg = resolve(
        workspace=_workspace(tmp_path),
        bsp_family="nxp",
        distro="fsl-imx-xwayland",  # CLI flag
    )

    assert cfg.distro == "fsl-imx-xwayland", f"CLI flag 'distro' must override {_DISTRO_VAR}"


# ---------------------------------------------------------------------------
# 2. Env var beats default
# ---------------------------------------------------------------------------


def test_env_machine_beats_default(tmp_path, monkeypatch):
    """Active machine env var must override the BSP-family default machine."""
    monkeypatch.setenv(_MACHINE_VAR, "imx8mm-var-dart")

    cfg = resolve(workspace=_workspace(tmp_path), bsp_family="nxp")

    assert cfg.machine == "imx8mm-var-dart", f"{_MACHINE_VAR} env var must beat default ({DEFAULT_NXP_MACHINE!r})"
    assert cfg.machine != DEFAULT_NXP_MACHINE


def test_env_manifest_beats_default(tmp_path, monkeypatch):
    """Active manifest env var must override the BSP-family default manifest."""
    monkeypatch.setenv(_MANIFEST_VAR, "imx-6.12.49-2.2.0.xml")

    cfg = resolve(workspace=_workspace(tmp_path), bsp_family="nxp")

    assert cfg.manifest == "imx-6.12.49-2.2.0.xml", (
        f"{_MANIFEST_VAR} env var must beat default ({DEFAULT_NXP_MANIFEST!r})"
    )


def test_env_image_beats_default(tmp_path, monkeypatch):
    """Active image env var must override the BSP-family default image."""
    monkeypatch.setenv(_IMAGE_VAR, "fsl-image-qt5")

    cfg = resolve(workspace=_workspace(tmp_path), bsp_family="nxp")

    assert cfg.image == "fsl-image-qt5", f"{_IMAGE_VAR} env var must beat the NXP default image"


def test_no_env_yields_default(tmp_path, monkeypatch):
    """Without CLI flags or env vars the BSP-family default is used."""
    monkeypatch.delenv(_MACHINE_VAR, raising=False)

    cfg = resolve(workspace=_workspace(tmp_path), bsp_family="nxp")

    assert cfg.machine == DEFAULT_NXP_MACHINE, "Absent env + no CLI flag must fall back to BSP-family default"
