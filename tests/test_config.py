"""Tests for :class:`bakar.config.BuildConfig` resolution.

Covers fields not exercised by ``tests/test_env_precedence.py`` -- in
particular the ``use_hashequiv`` flag threaded from ``UserConfig.hashserv``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bakar.config import compose_preset_output_path, resolve
from bakar.preset_config import PresetEntry
from bakar.user_config import UserConfig
from bakar.workspace_config import WorkspaceConfig

pytestmark = pytest.mark.unit


def _workspace(tmp_path):
    """Return a workspace path with the nxp subdir present."""
    (tmp_path / "nxp").mkdir(parents=True, exist_ok=True)
    return tmp_path


def test_resolve_use_hashequiv_default_false_without_user_config(tmp_path) -> None:
    """Without a user_config, ``use_hashequiv`` resolves to False."""
    cfg = resolve(workspace=_workspace(tmp_path), bsp_family="nxp")

    assert cfg.use_hashequiv is False


def test_resolve_use_hashequiv_threads_from_user_config_true(tmp_path) -> None:
    """``UserConfig(hashserv=True)`` threads to ``cfg.use_hashequiv is True``."""
    uc = UserConfig(hashserv=True)

    cfg = resolve(workspace=_workspace(tmp_path), bsp_family="nxp", user_config=uc)

    assert cfg.use_hashequiv is True


def test_resolve_use_hashequiv_threads_from_user_config_false(tmp_path) -> None:
    """``UserConfig(hashserv=False)`` threads to ``cfg.use_hashequiv is False``."""
    uc = UserConfig(hashserv=False)

    cfg = resolve(workspace=_workspace(tmp_path), bsp_family="nxp", user_config=uc)

    assert cfg.use_hashequiv is False


def test_effective_ccache_dir_per_workspace_by_default(tmp_path) -> None:
    """Without opting in, ccache is per-workspace at ``<workspace>/ccache``."""
    ws = _workspace(tmp_path)
    cfg = resolve(workspace=ws, bsp_family="nxp")

    assert cfg.effective_ccache_dir == ws.resolve() / "ccache"


def test_effective_ccache_dir_shared_uses_xdg_cache(tmp_path, monkeypatch) -> None:
    """``ccache_shared`` selects a single shared cache under XDG_CACHE_HOME."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    uc = UserConfig(ccache_shared=True)

    cfg = resolve(workspace=_workspace(tmp_path), bsp_family="nxp", user_config=uc)

    assert cfg.effective_ccache_dir == tmp_path / "xdg" / "bakar" / "ccache"


def test_effective_ccache_dir_explicit_path_wins(tmp_path) -> None:
    """An explicit ``ccache_dir`` is honored verbatim, over shared and default."""
    uc = UserConfig(ccache_shared=True, ccache_dir="/mnt/cache/cc")

    cfg = resolve(workspace=_workspace(tmp_path), bsp_family="nxp", user_config=uc)

    assert cfg.effective_ccache_dir == Path("/mnt/cache/cc")


# ---------------------------------------------------------------------------
# compose_preset_output_path tests
# ---------------------------------------------------------------------------


def test_compose_preset_output_path_nxp_single_release() -> None:
    """nxp single-release: version extracted from manifest filename."""
    preset = PresetEntry(
        name="imx8mp-scarthgap",
        family="nxp",
        machine="imx8mp-var-dart",
        distro="fsl-imx-xwayland",
        image="core-image-minimal",
        manifest="imx-6.6.52-2.2.2.xml",
        branch="scarthgap",
    )

    result = compose_preset_output_path(preset)

    assert result == "fsl-imx-xwayland-imx8mp-var-dart-6.6.52-2.2.2"


def test_compose_preset_output_path_nxp_multi_release() -> None:
    """nxp multi-release: version extracted from manifests[release_index]."""
    preset = PresetEntry(
        name="imx8mp-two-releases",
        family="nxp",
        machine="imx8mp-var-dart",
        distro="fsl-imx-xwayland",
        image="core-image-minimal",
        manifests=["imx-6.6.52-2.2.2.xml", "imx-6.12.3-1.0.0.xml"],
        branches=["scarthgap", "walnascar"],
    )

    result0 = compose_preset_output_path(preset, release_index=0)
    result1 = compose_preset_output_path(preset, release_index=1)

    assert result0 == "fsl-imx-xwayland-imx8mp-var-dart-6.6.52-2.2.2"
    assert result1 == "fsl-imx-xwayland-imx8mp-var-dart-6.12.3-1.0.0"


def test_compose_preset_output_path_ti_single_release() -> None:
    """ti single-release: version extracted from TI manifest filename."""
    preset = PresetEntry(
        name="am62x-scarthgap",
        family="ti",
        machine="am62x-var-som",
        distro="arago",
        image="var-thin-image",
        manifest="processor-sdk-scarthgap-chromium-11.00.09.04-config_var01.txt",
        branch="scarthgap_11.00.09.04_var01",
    )

    result = compose_preset_output_path(preset)

    assert "11.00.09.04" in result
    assert result.startswith("arago-am62x-var-som-")


def test_compose_preset_output_path_bbsetup_single_release() -> None:
    """bbsetup single-release: <image>-<machine> with no stem suffix."""
    preset = PresetEntry(
        name="avocado-qemux86-64",
        family="bbsetup",
        machine="qemux86-64",
        image="avocado-os",
        kas_yaml="kas/qemux86-64.yml",
    )

    result = compose_preset_output_path(preset)

    assert result == "avocado-os-qemux86-64"


def test_compose_preset_output_path_bbsetup_multi_release_distinct_stems() -> None:
    """bbsetup multi-release: distinct kas YAML stems produce distinct paths."""
    preset = PresetEntry(
        name="avocado-all-releases",
        family="bbsetup",
        machine="qemux86-64",
        image="avocado-os",
        kas_yamls=["kas/qemux86-64.yml", "kas/qemux86-64-lts.yml"],
    )

    result0 = compose_preset_output_path(preset, release_index=0)
    result1 = compose_preset_output_path(preset, release_index=1)

    assert result0 == "avocado-os-qemux86-64-qemux86-64"
    assert result1 == "avocado-os-qemux86-64-qemux86-64-lts"
    assert result0 != result1


def test_compose_preset_output_path_generic_single_release() -> None:
    """generic single-release: same format as bbsetup single-release."""
    preset = PresetEntry(
        name="generic-build",
        family="generic",
        machine="qemux86-64",
        image="core-image-minimal",
        kas_yaml="my-build.yml",
    )

    result = compose_preset_output_path(preset)

    assert result == "core-image-minimal-qemux86-64"


# ---------------------------------------------------------------------------
# resolve() with preset tier precedence tests
# ---------------------------------------------------------------------------


def test_resolve_preset_beats_user_config(tmp_path) -> None:
    """preset machine beats user-config machine."""
    preset = PresetEntry(
        name="test-preset",
        family="nxp",
        machine="preset-machine",
        manifest="imx-6.6.52-2.2.2.xml",
        branch="scarthgap",
    )
    uc = UserConfig(nxp_machine="user-machine")

    cfg = resolve(workspace=_workspace(tmp_path), bsp_family="nxp", preset=preset, user_config=uc)

    assert cfg.machine == "preset-machine"


def test_resolve_explicit_spec_beats_preset(tmp_path) -> None:
    """An explicit CLI flag (spec.machine) wins over the preset value."""
    from bakar.config import BSPSpec

    preset = PresetEntry(
        name="test-preset",
        family="nxp",
        machine="preset-machine",
        manifest="imx-6.6.52-2.2.2.xml",
        branch="scarthgap",
    )
    spec = BSPSpec(machine="cli-machine")

    cfg = resolve(workspace=_workspace(tmp_path), bsp_family="nxp", spec=spec, preset=preset)

    assert cfg.machine == "cli-machine"


def test_resolve_host_threshold_default_without_configs(tmp_path) -> None:
    """No user or workspace config -> host thresholds resolve to built-in defaults."""
    cfg = resolve(workspace=_workspace(tmp_path), bsp_family="nxp")

    assert cfg.host_inotify_instances == 4096
    assert cfg.host_inotify_watches == 524288
    assert cfg.host_swappiness_max == 20
    assert cfg.host_nofile_soft == 8192
    assert cfg.host_mem_min_gb == 16.0


def test_resolve_host_threshold_user_value_used_when_only_user_sets(tmp_path) -> None:
    """User config sets a host threshold and no workspace tier -> user value wins over default."""
    uc = UserConfig(host_inotify_instances=5000)

    cfg = resolve(workspace=_workspace(tmp_path), bsp_family="nxp", user_config=uc)

    assert cfg.host_inotify_instances == 5000


def test_resolve_host_threshold_workspace_wins_over_user(tmp_path) -> None:
    """Workspace [host] outranks user config.toml [host] for the same field."""
    uc = UserConfig(host_inotify_instances=5000)
    wc = WorkspaceConfig(host_inotify_instances=9000)

    cfg = resolve(
        workspace=_workspace(tmp_path),
        bsp_family="nxp",
        user_config=uc,
        workspace_config=wc,
    )

    assert cfg.host_inotify_instances == 9000


def test_resolve_host_threshold_user_used_when_workspace_unset(tmp_path) -> None:
    """A workspace config with the field None falls back to the user value."""
    uc = UserConfig(host_swappiness_max=10)
    wc = WorkspaceConfig()

    cfg = resolve(
        workspace=_workspace(tmp_path),
        bsp_family="nxp",
        user_config=uc,
        workspace_config=wc,
    )

    assert cfg.host_swappiness_max == 10


def test_resolve_host_mem_min_gb_stays_float(tmp_path) -> None:
    """``host_mem_min_gb`` resolves as a float through the selector."""
    uc = UserConfig(host_mem_min_gb=24.0)

    cfg = resolve(workspace=_workspace(tmp_path), bsp_family="nxp", user_config=uc)

    assert cfg.host_mem_min_gb == 24.0
    assert isinstance(cfg.host_mem_min_gb, float)
