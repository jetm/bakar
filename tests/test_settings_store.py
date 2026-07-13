"""Unit tests for the settings CRUD functions in bakar.user_config."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from bakar.user_config import (
    SETTINGS_SCHEMA,
    get_setting,
    list_settings,
    load_user_config,
    set_setting,
    unset_setting,
)

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


@pytest.mark.unit
def test_set_then_load_user_config_round_trip(tmp_path: Path) -> None:
    """Every recognized key written via set_setting reads back through load_user_config (A4)."""
    config_file = tmp_path / "config.toml"
    written = {
        "defaults.nxp.machine": "imx8mp-var-dart",
        "defaults.nxp.distro": "fsl-imx-xwayland",
        "defaults.nxp.image": "core-image-minimal",
        "defaults.nxp.manifest": "imx-6.6.52-2.2.2.xml",
        "defaults.nxp.repo_url": "https://github.com/varigit/variscite-bsp-platform.git",
        "defaults.ti.machine": "am62x-var-som",
        "defaults.ti.distro": "arago",
        "defaults.ti.image": "var-thin-image",
        "defaults.ti.manifest": "processor-sdk-scarthgap.txt",
        "build.kas_container_image": "jetm/kas-build-env:latest",
        "build.show_doctor_report": "false",
        "build.show_baseline_drift": "true",
        "build.dl_dir": "/data/dl",
        "build.sstate_dir": "/data/sstate",
        "build.sstate_mirror_url": "https://cache.example.com",
        "build.sstate_mirrors": "file:///mirror/sstate PATH",
        "build.scheduler": "completion",
        "build.pressure_max_cpu": "60",
        "build.pressure_max_io": "45",
        "build.pressure_max_memory": "20",
        "build.disk_free_threshold_gb": "75.0",
        "build.stall_abort_secs": "1800",
        "build.stop_grace_seconds": "45",
        "build.stop_on_error": "false",
        "build.hashserv": "true",
        "build.ccache_shared": "true",
        "build.ccache_dir": "/data/ccache",
        "build.buildtools_dir": "/some/dir",
        "build.psi_autocalibrate": "true",
        "build.sccache_dist": "true",
        "build.sccache_scheduler_url": "http://localhost:10600",
        "build.cluster_bind_host": "10.42.0.1",
        "build.bb_hashserve": "10.42.0.1:8686",
        "build.prserv_host": "10.42.0.1:8585",
        "build.cluster": "true",
        "build.ccache": "false",
        "build.rm_work": "true",
        "build.container": "true",
        "build.host_mode": "true",
        "build.nproc": "96",
        "build.parallel_make": "256",
        "build.bb_number_threads": "24",
        "layers.show_hashes": "true",
        "layers.show_sstate_summary": "true",
        "host.inotify_instances": "8192",
        "host.inotify_watches": "1048576",
        "host.swappiness_max": "10",
        "host.nofile_soft": "16384",
        "host.mem_min_gb": "32.0",
    }
    # Every dotted key in the schema is exercised here.
    assert set(written) == set(SETTINGS_SCHEMA)

    for key, value in written.items():
        set_setting(key, value, config_file)

    cfg = load_user_config(config_file)

    assert cfg.nxp_machine == "imx8mp-var-dart"
    assert cfg.nxp_distro == "fsl-imx-xwayland"
    assert cfg.nxp_image == "core-image-minimal"
    assert cfg.nxp_manifest == "imx-6.6.52-2.2.2.xml"
    assert cfg.nxp_repo_url == "https://github.com/varigit/variscite-bsp-platform.git"
    assert cfg.ti_machine == "am62x-var-som"
    assert cfg.ti_distro == "arago"
    assert cfg.ti_image == "var-thin-image"
    assert cfg.ti_manifest == "processor-sdk-scarthgap.txt"
    assert cfg.kas_container_image == "jetm/kas-build-env:latest"
    assert cfg.show_doctor_report is False
    assert cfg.dl_dir == "/data/dl"
    assert cfg.sstate_dir == "/data/sstate"
    assert cfg.sstate_mirror_url == "https://cache.example.com"
    assert cfg.sstate_mirrors == "file:///mirror/sstate PATH"
    assert cfg.scheduler == "completion"
    assert cfg.pressure_max_cpu == 60
    assert cfg.pressure_max_io == 45
    assert cfg.pressure_max_memory == 20
    assert cfg.disk_free_threshold_gb == 75.0
    assert cfg.stall_abort_secs == 1800
    assert cfg.stop_grace_seconds == 45
    assert cfg.stop_on_error is False
    assert cfg.hashserv is True
    assert cfg.ccache_shared is True
    assert cfg.ccache_dir == "/data/ccache"
    assert cfg.buildtools_dir == "/some/dir"
    assert cfg.psi_autocalibrate is True
    assert cfg.sccache_dist is True
    assert isinstance(cfg.sccache_dist, bool)
    assert cfg.sccache_scheduler_url == "http://localhost:10600"
    assert cfg.bb_hashserve == "10.42.0.1:8686"
    assert cfg.prserv_host == "10.42.0.1:8585"
    assert cfg.cluster_bind_host == "10.42.0.1"
    assert cfg.cluster is True
    assert isinstance(cfg.cluster, bool)
    assert cfg.ccache is False
    assert isinstance(cfg.ccache, bool)
    assert cfg.rm_work is True
    assert isinstance(cfg.rm_work, bool)
    assert cfg.container is True
    assert isinstance(cfg.container, bool)
    assert cfg.host_mode is True
    assert isinstance(cfg.host_mode, bool)
    assert cfg.nproc == 96
    assert isinstance(cfg.nproc, int) and not isinstance(cfg.nproc, bool)
    assert cfg.parallel_make == 256
    assert cfg.bb_number_threads == 24
    assert cfg.show_hashes is True
    assert cfg.show_sstate_summary is True
    assert cfg.host_inotify_instances == 8192
    assert isinstance(cfg.host_inotify_instances, int) and not isinstance(cfg.host_inotify_instances, bool)
    assert cfg.host_inotify_watches == 1048576
    assert isinstance(cfg.host_inotify_watches, int) and not isinstance(cfg.host_inotify_watches, bool)
    assert cfg.host_swappiness_max == 10
    assert isinstance(cfg.host_swappiness_max, int) and not isinstance(cfg.host_swappiness_max, bool)
    assert cfg.host_nofile_soft == 16384
    assert isinstance(cfg.host_nofile_soft, int) and not isinstance(cfg.host_nofile_soft, bool)
    assert cfg.host_mem_min_gb == 32.0
    assert isinstance(cfg.host_mem_min_gb, float)


@pytest.mark.unit
def test_set_bool_key_stores_boolean_not_string(tmp_path: Path) -> None:
    """`build.show_doctor_report false` coerces to a real bool, not the string "false"."""
    config_file = tmp_path / "config.toml"
    set_setting("build.show_doctor_report", "false", config_file)

    stored = get_setting("build.show_doctor_report", config_file)
    assert stored is False
    assert isinstance(stored, bool)

    # load_user_config also reads it back as a typed bool, not a string.
    assert load_user_config(config_file).show_doctor_report is False


@pytest.mark.unit
def test_set_stall_abort_secs_stores_int_not_string(tmp_path: Path) -> None:
    """`build.stall_abort_secs 1800` coerces to a real int, including 0 (disabled)."""
    config_file = tmp_path / "config.toml"
    set_setting("build.stall_abort_secs", "1800", config_file)
    stored = get_setting("build.stall_abort_secs", config_file)
    assert stored == 1800
    assert isinstance(stored, int) and not isinstance(stored, bool)

    set_setting("build.stall_abort_secs", "0", config_file)
    assert load_user_config(config_file).stall_abort_secs == 0


@pytest.mark.unit
def test_set_stall_abort_secs_rejects_negative(tmp_path: Path) -> None:
    """A negative stall timeout is meaningless and rejected at coerce time."""
    config_file = tmp_path / "config.toml"
    with pytest.raises(ValueError, match="stall_abort_secs"):
        set_setting("build.stall_abort_secs", "-1", config_file)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [("true", True), ("1", True), ("false", False), ("0", False)],
)
def test_set_bool_key_accepts_all_literals(tmp_path: Path, raw: str, expected: bool) -> None:
    config_file = tmp_path / "config.toml"
    set_setting("layers.show_hashes", raw, config_file)
    stored = get_setting("layers.show_hashes", config_file)
    assert stored is expected
    assert isinstance(stored, bool)


@pytest.mark.unit
def test_set_unrecognized_key_raises_without_writing(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    with pytest.raises(ValueError, match=r"not\.a\.real\.key"):
        set_setting("not.a.real.key", "value", config_file)
    assert not config_file.exists()


@pytest.mark.unit
def test_set_non_bool_value_on_bool_key_raises_without_writing(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    with pytest.raises(ValueError, match="maybe"):
        set_setting("build.show_doctor_report", "maybe", config_file)
    assert not config_file.exists()


@pytest.mark.unit
def test_get_recognized_but_absent_key_returns_none(tmp_path: Path) -> None:
    """A recognized key that is not in the file (or no file at all) reads as None."""
    config_file = tmp_path / "config.toml"
    # No file on disk yet.
    assert get_setting("defaults.nxp.machine", config_file) is None

    # File exists but does not contain this key.
    set_setting("defaults.ti.machine", "am62x-var-som", config_file)
    assert get_setting("defaults.nxp.machine", config_file) is None


@pytest.mark.unit
def test_get_unrecognized_key_raises(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    with pytest.raises(ValueError, match=r"not\.a\.real\.key"):
        get_setting("not.a.real.key", config_file)


@pytest.mark.unit
def test_unset_removes_only_target_key(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    set_setting("defaults.nxp.machine", "imx8mp-var-dart", config_file)
    set_setting("defaults.nxp.distro", "fsl-imx-xwayland", config_file)

    unset_setting("defaults.nxp.machine", config_file)

    assert get_setting("defaults.nxp.machine", config_file) is None
    assert get_setting("defaults.nxp.distro", config_file) == "fsl-imx-xwayland"
    # The sibling key survives a reload, proving the table was not dropped.
    assert load_user_config(config_file).nxp_distro == "fsl-imx-xwayland"


@pytest.mark.unit
def test_unset_absent_key_is_noop_leaving_well_formed_file(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    set_setting("defaults.ti.machine", "am62x-var-som", config_file)
    before = config_file.read_bytes()

    # Target key was never set; unset must not alter or corrupt the file.
    unset_setting("defaults.nxp.machine", config_file)

    assert config_file.read_bytes() == before
    # File is still parseable and the unrelated key is intact.
    assert load_user_config(config_file).ti_machine == "am62x-var-som"


@pytest.mark.unit
def test_unset_unrecognized_key_raises(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    with pytest.raises(ValueError, match=r"not\.a\.real\.key"):
        unset_setting("not.a.real.key", config_file)


@pytest.mark.unit
def test_list_settings_reports_set_and_unset_keys(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    set_setting("defaults.nxp.machine", "imx8mp-var-dart", config_file)
    set_setting("build.show_doctor_report", "false", config_file)

    settings = list_settings(config_file)

    # Every schema key is present in the mapping.
    assert set(settings) == set(SETTINGS_SCHEMA)
    assert settings["defaults.nxp.machine"] == "imx8mp-var-dart"
    assert settings["build.show_doctor_report"] is False
    # An unset key maps to None.
    assert settings["defaults.ti.machine"] is None


@pytest.mark.unit
def test_list_settings_with_no_file_reports_all_unset(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    settings = list_settings(config_file)
    assert set(settings) == set(SETTINGS_SCHEMA)
    assert all(value is None for value in settings.values())
