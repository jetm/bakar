"""Tests for env var precedence rules (spec: env-var-namespace).

Verifies the resolution stack in :func:`bakar.config.resolve`:

1. CLI flag (explicit arg) beats env var.
2. Env var beats the ``user_config`` field.
3. ``user_config`` field beats the BSP-family default.
"""

from __future__ import annotations

import pytest

from bakar.config import (
    DEFAULT_CONTAINER_IMAGE,
    DEFAULT_NXP_MACHINE,
    DEFAULT_NXP_MANIFEST,
    BSPSpec,
    resolve,
)
from bakar.user_config import UserConfig
from bakar.workspace_config import write_workspace_config

pytestmark = pytest.mark.unit

_MACHINE_VAR = "BAKAR_MACHINE"
_MANIFEST_VAR = "BAKAR_MANIFEST"
_DISTRO_VAR = "BAKAR_DISTRO"
_IMAGE_VAR = "BAKAR_IMAGE"


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
        spec=BSPSpec(machine="my-board"),  # CLI flag
    )

    assert cfg.machine == "my-board", f"CLI flag 'machine' must override {_MACHINE_VAR}"


def test_cli_manifest_beats_env(tmp_path, monkeypatch):
    """Explicit manifest arg must win over the active manifest env var."""
    monkeypatch.setenv(_MANIFEST_VAR, "imx-6.12.49-2.2.0.xml")

    cfg = resolve(
        workspace=_workspace(tmp_path),
        bsp_family="nxp",
        spec=BSPSpec(manifest="imx-6.6.52-2.2.2.xml"),  # CLI flag
    )

    assert cfg.manifest == "imx-6.6.52-2.2.2.xml", f"CLI flag 'manifest' must override {_MANIFEST_VAR}"


def test_cli_distro_beats_env(tmp_path, monkeypatch):
    """Explicit distro arg must win over the active distro env var."""
    monkeypatch.setenv(_DISTRO_VAR, "fsl-imx-wayland")

    cfg = resolve(
        workspace=_workspace(tmp_path),
        bsp_family="nxp",
        spec=BSPSpec(distro="fsl-imx-xwayland"),  # CLI flag
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


# ---------------------------------------------------------------------------
# 3. KAS_CONTAINER_IMAGE -> host_mode auto-detection
# ---------------------------------------------------------------------------


def test_host_mode_auto_enables_when_kas_container_image_absent(tmp_path, monkeypatch):
    """Absent KAS_CONTAINER_IMAGE must auto-enable host_mode."""
    monkeypatch.delenv("KAS_CONTAINER_IMAGE", raising=False)

    cfg = resolve(workspace=_workspace(tmp_path), bsp_family="nxp")

    assert cfg.host_mode is True, "host_mode must auto-enable when KAS_CONTAINER_IMAGE is absent"


def test_host_mode_false_when_kas_container_image_set(tmp_path, monkeypatch):
    """With KAS_CONTAINER_IMAGE set, host_mode stays False."""
    monkeypatch.setenv("KAS_CONTAINER_IMAGE", "test/kas-image:latest")

    cfg = resolve(workspace=_workspace(tmp_path), bsp_family="nxp")

    assert cfg.host_mode is False, "host_mode must be False when KAS_CONTAINER_IMAGE is configured"


def test_explicit_host_mode_beats_kas_container_image(tmp_path, monkeypatch):
    """Explicit host_mode=True wins even when KAS_CONTAINER_IMAGE is set."""
    monkeypatch.setenv("KAS_CONTAINER_IMAGE", "test/kas-image:latest")

    cfg = resolve(workspace=_workspace(tmp_path), bsp_family="nxp", spec=BSPSpec(host_mode=True))

    assert cfg.host_mode is True, "Explicit host_mode=True must override KAS_CONTAINER_IMAGE presence"


# ---------------------------------------------------------------------------
# 4. user_config tier (config.toml values)
# ---------------------------------------------------------------------------


def test_user_config_machine_beats_default(tmp_path, monkeypatch):
    """A user_config field must override the BSP-family default when no env/CLI is set."""
    monkeypatch.delenv(_MACHINE_VAR, raising=False)
    uc = UserConfig(nxp_machine="imx93-var-som")

    cfg = resolve(workspace=_workspace(tmp_path), bsp_family="nxp", user_config=uc)

    assert cfg.machine == "imx93-var-som", "user_config.nxp_machine must beat the built-in default"
    assert cfg.machine != DEFAULT_NXP_MACHINE


def test_env_machine_beats_user_config(tmp_path, monkeypatch):
    """An env var must override the matching user_config field."""
    monkeypatch.setenv(_MACHINE_VAR, "env-board")
    uc = UserConfig(nxp_machine="config-board")

    cfg = resolve(workspace=_workspace(tmp_path), bsp_family="nxp", user_config=uc)

    assert cfg.machine == "env-board", f"{_MACHINE_VAR} env var must beat user_config.nxp_machine"


def test_cli_machine_beats_user_config(tmp_path, monkeypatch):
    """An explicit CLI arg must override the matching user_config field."""
    monkeypatch.delenv(_MACHINE_VAR, raising=False)
    uc = UserConfig(nxp_machine="config-board")

    cfg = resolve(
        workspace=_workspace(tmp_path),
        bsp_family="nxp",
        spec=BSPSpec(machine="cli-board"),  # CLI flag
        user_config=uc,
    )

    assert cfg.machine == "cli-board", "CLI flag 'machine' must beat user_config.nxp_machine"


def test_user_config_container_image_used_when_env_absent(tmp_path, monkeypatch):
    """user_config.container_image is used when KAS_CONTAINER_IMAGE is unset."""
    monkeypatch.delenv("KAS_CONTAINER_IMAGE", raising=False)
    uc = UserConfig(container_image="config/kas-image:latest")

    cfg = resolve(workspace=_workspace(tmp_path), bsp_family="nxp", user_config=uc)

    assert cfg.container_image == "config/kas-image:latest", (
        "user_config.container_image must be used when KAS_CONTAINER_IMAGE is unset"
    )
    assert cfg.container_image != DEFAULT_CONTAINER_IMAGE
    assert cfg.host_mode is False, "A config-supplied container_image must disable host_mode auto-enable"


def test_env_container_image_beats_user_config(tmp_path, monkeypatch):
    """KAS_CONTAINER_IMAGE env var must override user_config.container_image."""
    monkeypatch.setenv("KAS_CONTAINER_IMAGE", "env/kas-image:latest")
    uc = UserConfig(container_image="config/kas-image:latest")

    cfg = resolve(workspace=_workspace(tmp_path), bsp_family="nxp", user_config=uc)

    assert cfg.container_image == "env/kas-image:latest", (
        "KAS_CONTAINER_IMAGE env var must beat user_config.container_image"
    )


# ---------------------------------------------------------------------------
# 5. Build-tuning fields (dl_dir, sstate_dir, pressure_max_*)
# ---------------------------------------------------------------------------


def test_user_config_sstate_dir_reaches_resolved_config(tmp_path, monkeypatch) -> None:
    """user_config.sstate_dir is threaded onto BuildConfig when the env var is unset."""
    monkeypatch.delenv("SSTATE_DIR", raising=False)
    uc = UserConfig(sstate_dir="/data/sstate")

    cfg = resolve(workspace=_workspace(tmp_path), bsp_family="nxp", user_config=uc)

    assert cfg.sstate_dir == "/data/sstate"


def test_user_config_dl_dir_reaches_resolved_config(tmp_path, monkeypatch) -> None:
    """user_config.dl_dir is threaded onto BuildConfig when the env var is unset."""
    monkeypatch.delenv("DL_DIR", raising=False)
    uc = UserConfig(dl_dir="/data/dl")

    cfg = resolve(workspace=_workspace(tmp_path), bsp_family="nxp", user_config=uc)

    assert cfg.dl_dir == "/data/dl"


def test_user_config_pressure_max_integers_survive_resolution(tmp_path) -> None:
    """pressure_max_cpu/io/memory ints from user_config are preserved as ints on BuildConfig."""
    uc = UserConfig(pressure_max_cpu=60, pressure_max_io=45, pressure_max_memory=20)

    cfg = resolve(workspace=_workspace(tmp_path), bsp_family="nxp", user_config=uc)

    assert cfg.pressure_max_cpu == 60
    assert isinstance(cfg.pressure_max_cpu, int)
    assert cfg.pressure_max_io == 45
    assert isinstance(cfg.pressure_max_io, int)
    assert cfg.pressure_max_memory == 20
    assert isinstance(cfg.pressure_max_memory, int)


def test_no_user_config_yields_none_tuning_fields(tmp_path) -> None:
    """Without a user_config, all build-tuning fields are None on BuildConfig."""
    cfg = resolve(workspace=_workspace(tmp_path), bsp_family="nxp")

    assert cfg.dl_dir is None
    assert cfg.sstate_dir is None
    assert cfg.sstate_mirrors is None
    assert cfg.scheduler is None
    assert cfg.pressure_max_cpu is None
    assert cfg.pressure_max_io is None
    assert cfg.pressure_max_memory is None


# ---------------------------------------------------------------------------
# 6. Workspace tier (.bakar.toml values)
#
# These tests exercise the lazy auto-load path inside resolve(): they write a
# real .bakar.toml in tmp_path via write_workspace_config() and call resolve()
# with workspace=tmp_path, without passing workspace_config explicitly. The
# loader inside resolve() reads the file from the workspace path.
# ---------------------------------------------------------------------------


def test_workspace_machine_beats_user_config(tmp_path, monkeypatch):
    """A workspace .bakar.toml value must override the matching user_config field."""
    monkeypatch.delenv(_MACHINE_VAR, raising=False)
    write_workspace_config(tmp_path, "nxp", {"machine": "workspace-board"})
    uc = UserConfig(nxp_machine="config-board")

    cfg = resolve(workspace=_workspace(tmp_path), bsp_family="nxp", user_config=uc)

    assert cfg.machine == "workspace-board", "workspace .bakar.toml machine must beat user_config.nxp_machine"


def test_env_machine_beats_workspace(tmp_path, monkeypatch):
    """An env var must override the matching workspace .bakar.toml value."""
    monkeypatch.setenv(_MACHINE_VAR, "env-board")
    write_workspace_config(tmp_path, "nxp", {"machine": "workspace-board"})

    cfg = resolve(workspace=_workspace(tmp_path), bsp_family="nxp")

    assert cfg.machine == "env-board", f"{_MACHINE_VAR} env var must beat the workspace .bakar.toml machine"


def test_cli_machine_beats_workspace(tmp_path, monkeypatch):
    """An explicit CLI arg must override the matching workspace .bakar.toml value."""
    monkeypatch.delenv(_MACHINE_VAR, raising=False)
    write_workspace_config(tmp_path, "nxp", {"machine": "workspace-board"})

    cfg = resolve(
        workspace=_workspace(tmp_path),
        bsp_family="nxp",
        spec=BSPSpec(machine="cli-board"),  # CLI flag
    )

    assert cfg.machine == "cli-board", "CLI flag 'machine' must beat the workspace .bakar.toml machine"


def test_workspace_machine_beats_default(tmp_path, monkeypatch):
    """A workspace value must override the built-in default when user config is also absent."""
    monkeypatch.delenv(_MACHINE_VAR, raising=False)
    write_workspace_config(tmp_path, "nxp", {"machine": "workspace-board"})

    cfg = resolve(workspace=_workspace(tmp_path), bsp_family="nxp")

    assert cfg.machine == "workspace-board", "workspace .bakar.toml machine must beat the built-in default"
    assert cfg.machine != DEFAULT_NXP_MACHINE


def test_workspace_absent_falls_through_to_user_config(tmp_path, monkeypatch):
    """With no .bakar.toml present, the user_config field is used."""
    monkeypatch.delenv(_MACHINE_VAR, raising=False)
    assert not (tmp_path / ".bakar.toml").exists()
    uc = UserConfig(nxp_machine="config-board")

    cfg = resolve(workspace=_workspace(tmp_path), bsp_family="nxp", user_config=uc)

    assert cfg.machine == "config-board", "absent workspace .bakar.toml must fall through to user_config.nxp_machine"
