"""Unit tests for the settings CRUD functions in bspctl.user_config."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from bspctl.user_config import (
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
        "build.container_image": "jetm/kas-build-env:latest",
        "build.doctor": "false",
        "build.dl_dir": "/data/dl",
        "build.sstate_dir": "/data/sstate",
        "build.sstate_mirrors": "file:///mirror/sstate PATH",
        "build.scheduler": "completion",
        "build.pressure_max_cpu": "60",
        "build.pressure_max_io": "45",
        "build.pressure_max_memory": "20",
        "build.hashserv": "true",
        "layers.show_hashes": "true",
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
    assert cfg.container_image == "jetm/kas-build-env:latest"
    assert cfg.doctor is False
    assert cfg.dl_dir == "/data/dl"
    assert cfg.sstate_dir == "/data/sstate"
    assert cfg.sstate_mirrors == "file:///mirror/sstate PATH"
    assert cfg.scheduler == "completion"
    assert cfg.pressure_max_cpu == 60
    assert cfg.pressure_max_io == 45
    assert cfg.pressure_max_memory == 20
    assert cfg.hashserv is True
    assert cfg.show_hashes is True


@pytest.mark.unit
def test_set_bool_key_stores_boolean_not_string(tmp_path: Path) -> None:
    """`build.doctor false` coerces to a real bool, not the string "false"."""
    config_file = tmp_path / "config.toml"
    set_setting("build.doctor", "false", config_file)

    stored = get_setting("build.doctor", config_file)
    assert stored is False
    assert isinstance(stored, bool)

    # load_user_config also reads it back as a typed bool, not a string.
    assert load_user_config(config_file).doctor is False


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
    with pytest.raises(ValueError, match="not.a.real.key"):
        set_setting("not.a.real.key", "value", config_file)
    assert not config_file.exists()


@pytest.mark.unit
def test_set_non_bool_value_on_bool_key_raises_without_writing(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    with pytest.raises(ValueError, match="maybe"):
        set_setting("build.doctor", "maybe", config_file)
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
    with pytest.raises(ValueError, match="not.a.real.key"):
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
    with pytest.raises(ValueError, match="not.a.real.key"):
        unset_setting("not.a.real.key", config_file)


@pytest.mark.unit
def test_list_settings_reports_set_and_unset_keys(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    set_setting("defaults.nxp.machine", "imx8mp-var-dart", config_file)
    set_setting("build.doctor", "false", config_file)

    settings = list_settings(config_file)

    # Every schema key is present in the mapping.
    assert set(settings) == set(SETTINGS_SCHEMA)
    assert settings["defaults.nxp.machine"] == "imx8mp-var-dart"
    assert settings["build.doctor"] is False
    # An unset key maps to None.
    assert settings["defaults.ti.machine"] is None


@pytest.mark.unit
def test_list_settings_with_no_file_reports_all_unset(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    settings = list_settings(config_file)
    assert set(settings) == set(SETTINGS_SCHEMA)
    assert all(value is None for value in settings.values())
