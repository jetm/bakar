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


def test_hashserv_state_key_uses_sstate_dir(tmp_path, monkeypatch) -> None:
    """When an sstate dir is configured, the daemon keys to it (not bsp_root)."""
    monkeypatch.delenv("SSTATE_DIR", raising=False)
    uc = UserConfig(sstate_dir="/mnt/cache/sstate")

    cfg = resolve(workspace=_workspace(tmp_path), bsp_family="nxp", user_config=uc)

    assert cfg.hashserv_state_key == Path("/mnt/cache/sstate")


def test_hashserv_state_key_falls_back_to_bsp_root(tmp_path, monkeypatch) -> None:
    """With no sstate dir set, the daemon stays per-workspace at bsp_root."""
    monkeypatch.delenv("SSTATE_DIR", raising=False)
    cfg = resolve(workspace=_workspace(tmp_path), bsp_family="nxp")

    assert cfg.hashserv_state_key == cfg.bsp_root


def test_hashserv_state_key_resolves_relative_path(tmp_path, monkeypatch) -> None:
    """A relative sstate dir is resolved to an absolute path so the daemon's
    state location (and derived port) does not depend on the CLI's CWD."""
    monkeypatch.delenv("SSTATE_DIR", raising=False)
    uc = UserConfig(sstate_dir="rel/sstate")

    cfg = resolve(workspace=_workspace(tmp_path), bsp_family="nxp", user_config=uc)

    assert cfg.hashserv_state_key.is_absolute()
    assert cfg.hashserv_state_key == (Path("rel/sstate").resolve())


def test_hashserv_state_key_env_beats_config(tmp_path, monkeypatch) -> None:
    """A live SSTATE_DIR env var wins over the config value, matching the dir
    the build actually writes sstate to (see _build_env's setdefault)."""
    monkeypatch.setenv("SSTATE_DIR", "/mnt/env/sstate")
    uc = UserConfig(sstate_dir="/mnt/config/sstate")

    cfg = resolve(workspace=_workspace(tmp_path), bsp_family="nxp", user_config=uc)

    assert cfg.hashserv_state_key == Path("/mnt/env/sstate")


def test_hashserv_state_key_shared_across_workspaces_same_sstate(tmp_path, monkeypatch) -> None:
    """Two distinct workspaces sharing one sstate dir resolve to the same state
    key, so they share one daemon and one hash-equivalence DB."""
    monkeypatch.delenv("SSTATE_DIR", raising=False)
    uc = UserConfig(sstate_dir="/mnt/cache/sstate")
    (tmp_path / "wsA" / "nxp").mkdir(parents=True)
    (tmp_path / "wsB" / "nxp").mkdir(parents=True)

    cfg_a = resolve(workspace=tmp_path / "wsA", bsp_family="nxp", user_config=uc)
    cfg_b = resolve(workspace=tmp_path / "wsB", bsp_family="nxp", user_config=uc)

    assert cfg_a.bsp_root != cfg_b.bsp_root
    assert cfg_a.hashserv_state_key == cfg_b.hashserv_state_key


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


def test_resolve_ccache_default_true_rm_work_default_false(tmp_path) -> None:
    """Without configs, ccache defaults on and rm_work defaults off."""
    cfg = resolve(workspace=_workspace(tmp_path), bsp_family="nxp")

    assert cfg.ccache is True
    assert cfg.rm_work is False


def test_resolve_stop_on_error_default_true(tmp_path) -> None:
    """Without configs, stop_on_error defaults on (mirrors stall_abort_secs's enabled-by-default shape)."""
    cfg = resolve(workspace=_workspace(tmp_path), bsp_family="nxp")

    assert cfg.stop_on_error is True


def test_resolve_stop_on_error_user_config_disables(tmp_path) -> None:
    """Global config.toml [build] stop_on_error=false falls back to bitbake's natural drain."""
    cfg = resolve(workspace=_workspace(tmp_path), bsp_family="nxp", user_config=UserConfig(stop_on_error=False))

    assert cfg.stop_on_error is False


def test_resolve_stop_grace_seconds_default_thirty(tmp_path) -> None:
    """Without configs, stop_grace_seconds defaults to 30s (bounded wait).

    The 30s default bounds `bakar stop` so a wedged cooker cannot deadlock it
    when no operator is present to press Ctrl-C; 0 restores the unbounded wait.
    """
    cfg = resolve(workspace=_workspace(tmp_path), bsp_family="nxp")

    assert cfg.stop_grace_seconds == 30


def test_resolve_stop_grace_seconds_user_config_overrides(tmp_path) -> None:
    """Global config.toml [build] stop_grace_seconds flows through to BuildConfig."""
    cfg = resolve(workspace=_workspace(tmp_path), bsp_family="nxp", user_config=UserConfig(stop_grace_seconds=45))

    assert cfg.stop_grace_seconds == 45


def test_resolve_ccache_user_config_disables(tmp_path) -> None:
    """Global config.toml [build] ccache=false disables ccache."""
    cfg = resolve(workspace=_workspace(tmp_path), bsp_family="nxp", user_config=UserConfig(ccache=False))

    assert cfg.ccache is False


def test_resolve_rm_work_user_config_enables(tmp_path) -> None:
    """Global config.toml [build] rm_work=true keeps rm_work on."""
    cfg = resolve(workspace=_workspace(tmp_path), bsp_family="nxp", user_config=UserConfig(rm_work=True))

    assert cfg.rm_work is True


def test_resolve_ccache_workspace_wins_over_user(tmp_path) -> None:
    """Workspace [build] ccache outranks the global config for the same field."""
    cfg = resolve(
        workspace=_workspace(tmp_path),
        bsp_family="nxp",
        user_config=UserConfig(ccache=True),
        workspace_config=WorkspaceConfig(ccache=False),
    )

    assert cfg.ccache is False


def test_resolve_rm_work_workspace_wins_over_user(tmp_path) -> None:
    """Workspace [build] rm_work outranks the global config for the same field."""
    cfg = resolve(
        workspace=_workspace(tmp_path),
        bsp_family="nxp",
        user_config=UserConfig(rm_work=False),
        workspace_config=WorkspaceConfig(rm_work=True),
    )

    assert cfg.rm_work is True


def test_resolve_rm_work_env_wins_over_workspace(tmp_path, monkeypatch) -> None:
    """BAKAR_RM_WORK env beats both workspace and global config."""
    monkeypatch.setenv("BAKAR_RM_WORK", "1")
    cfg = resolve(
        workspace=_workspace(tmp_path),
        bsp_family="nxp",
        user_config=UserConfig(rm_work=False),
        workspace_config=WorkspaceConfig(rm_work=False),
    )

    assert cfg.rm_work is True


def test_resolve_ccache_env_wins_over_workspace(tmp_path, monkeypatch) -> None:
    """BAKAR_CCACHE env beats both workspace and global config."""
    monkeypatch.setenv("BAKAR_CCACHE", "0")
    cfg = resolve(
        workspace=_workspace(tmp_path),
        bsp_family="nxp",
        user_config=UserConfig(ccache=True),
        workspace_config=WorkspaceConfig(ccache=True),
    )

    assert cfg.ccache is False


def test_resolve_sccache_dist_user_config_toggles(tmp_path) -> None:
    """A single global config.toml [build] sccache_dist toggle flips sccache on and off."""
    on = resolve(workspace=_workspace(tmp_path), bsp_family="nxp", user_config=UserConfig(sccache_dist=True))
    off = resolve(workspace=_workspace(tmp_path), bsp_family="nxp", user_config=UserConfig(sccache_dist=False))

    assert on.use_sccache_dist is True
    assert off.use_sccache_dist is False


def test_resolve_sccache_dist_env_disables_over_user_config(tmp_path, monkeypatch) -> None:
    """BAKAR_SCCACHE_DIST=0 disables sccache even when the global config enables it."""
    monkeypatch.setenv("BAKAR_SCCACHE_DIST", "0")
    cfg = resolve(workspace=_workspace(tmp_path), bsp_family="nxp", user_config=UserConfig(sccache_dist=True))

    assert cfg.use_sccache_dist is False


def test_resolve_sccache_dist_env_enables_over_user_config(tmp_path, monkeypatch) -> None:
    """BAKAR_SCCACHE_DIST=1 enables sccache even when the global config disables it."""
    monkeypatch.setenv("BAKAR_SCCACHE_DIST", "1")
    cfg = resolve(workspace=_workspace(tmp_path), bsp_family="nxp", user_config=UserConfig(sccache_dist=False))

    assert cfg.use_sccache_dist is True


def test_resolve_use_ccache_false_when_sccache_dist_active(tmp_path) -> None:
    """ccache and sccache are mutually exclusive: use_ccache is False under sccache-dist."""
    cfg = resolve(
        workspace=_workspace(tmp_path),
        bsp_family="nxp",
        user_config=UserConfig(ccache=True, sccache_dist=True),
    )

    assert cfg.ccache is True
    assert cfg.use_sccache_dist is True
    assert cfg.use_ccache is False


def test_resolve_use_ccache_true_when_no_sccache(tmp_path) -> None:
    """use_ccache is True when ccache is on and sccache-dist is off."""
    cfg = resolve(workspace=_workspace(tmp_path), bsp_family="nxp", user_config=UserConfig(ccache=True))

    assert cfg.use_ccache is True


def test_resolve_use_ccache_false_when_ccache_disabled(tmp_path) -> None:
    """use_ccache is False when ccache is explicitly disabled, regardless of sccache."""
    cfg = resolve(workspace=_workspace(tmp_path), bsp_family="nxp", user_config=UserConfig(ccache=False))

    assert cfg.use_ccache is False


# ---------------------------------------------------------------------------
# resolve() bsp_family None sentinel and preset/family conflict tests
# ---------------------------------------------------------------------------


def test_resolve_omitted_bsp_family_takes_preset_family(tmp_path) -> None:
    """Omitting bsp_family with an active non-nxp preset resolves to the preset's family."""
    preset = PresetEntry(
        name="ti-test-preset",
        family="ti",
        manifest="processor-sdk-scarthgap-chromium-11.00.09.04-config_var01.txt",
        branch="scarthgap_11.00.09.04_var01",
    )

    cfg = resolve(workspace=_workspace(tmp_path), preset=preset)

    assert cfg.bsp_family == "ti"


def test_resolve_omitted_bsp_family_defaults_to_nxp_without_preset(tmp_path) -> None:
    """Omitting bsp_family with no active preset still defaults to nxp."""
    cfg = resolve(workspace=_workspace(tmp_path))

    assert cfg.bsp_family == "nxp"


def test_resolve_explicit_bsp_family_conflicting_with_preset_raises(tmp_path) -> None:
    """An explicit bsp_family that disagrees with the active preset's family is a loud error."""
    preset = PresetEntry(
        name="ti-test-preset",
        family="ti",
        manifest="processor-sdk-scarthgap-chromium-11.00.09.04-config_var01.txt",
        branch="scarthgap_11.00.09.04_var01",
    )

    with pytest.raises(ValueError, match="ti-test-preset") as excinfo:
        resolve(workspace=_workspace(tmp_path), bsp_family="nxp", preset=preset)

    message = str(excinfo.value)
    assert "nxp" in message
    assert "ti" in message


def test_resolve_explicit_bsp_family_matching_preset_succeeds(tmp_path) -> None:
    """An explicit bsp_family that matches the active preset's family raises nothing."""
    preset = PresetEntry(
        name="ti-test-preset",
        family="ti",
        manifest="processor-sdk-scarthgap-chromium-11.00.09.04-config_var01.txt",
        branch="scarthgap_11.00.09.04_var01",
    )

    cfg = resolve(workspace=_workspace(tmp_path), bsp_family="ti", preset=preset)

    assert cfg.bsp_family == "ti"


def test_resolve_fallback_bsp_family_conflicting_with_preset_defers_silently(tmp_path) -> None:
    """A fallback-derived bsp_family conflicting with an active preset defers, no error."""
    preset = PresetEntry(
        name="ti-test-preset",
        family="ti",
        manifest="processor-sdk-scarthgap-chromium-11.00.09.04-config_var01.txt",
        branch="scarthgap_11.00.09.04_var01",
    )

    cfg = resolve(workspace=_workspace(tmp_path), bsp_family="nxp", preset=preset, family_is_explicit=False)

    assert cfg.bsp_family == "ti"


def test_resolve_default_family_is_explicit_preserves_conflict_raise(tmp_path) -> None:
    """Omitting family_is_explicit still raises for an explicit conflicting bsp_family."""
    preset = PresetEntry(
        name="ti-test-preset",
        family="ti",
        manifest="processor-sdk-scarthgap-chromium-11.00.09.04-config_var01.txt",
        branch="scarthgap_11.00.09.04_var01",
    )

    with pytest.raises(ValueError, match="ti-test-preset"):
        resolve(workspace=_workspace(tmp_path), bsp_family="nxp", preset=preset)
