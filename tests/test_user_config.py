"""Unit tests for bakar.user_config."""

from __future__ import annotations

import re
import textwrap
from typing import TYPE_CHECKING

import pytest

from bakar.user_config import UserConfig, load_user_config, set_setting

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


@pytest.mark.unit
def test_missing_file_returns_defaults(tmp_path: Path) -> None:
    result = load_user_config(tmp_path / "nonexistent.toml")
    assert result == UserConfig()
    assert result.nxp_machine is None
    assert result.ti_manifest is None
    assert result.container_image is None
    assert result.doctor is True
    assert result.show_hashes is False


@pytest.mark.unit
def test_full_file_populates_every_field(tmp_path: Path) -> None:
    toml_content = textwrap.dedent("""\
        [defaults.nxp]
        machine  = "imx8mp-var-dart"
        distro   = "fsl-imx-xwayland"
        image    = "core-image-minimal"
        manifest = "imx-6.6.52-2.2.2.xml"
        repo_url = "https://github.com/varigit/variscite-bsp-platform.git"

        [defaults.ti]
        machine  = "am62x-var-som"
        distro   = "arago"
        image    = "var-thin-image"
        manifest = "processor-sdk-scarthgap.txt"

        [build]
        container_image = "jetm/kas-build-env:latest"
        doctor          = false

        [layers]
        show_hashes = true
    """)
    config_file = tmp_path / "config.toml"
    config_file.write_text(toml_content)

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
    assert cfg.show_hashes is True


@pytest.mark.unit
def test_partial_file_leaves_unsupplied_fields_at_defaults(tmp_path: Path) -> None:
    toml_content = textwrap.dedent("""\
        [defaults.nxp]
        machine = "imx93-var-som"

        [layers]
        show_hashes = true
    """)
    config_file = tmp_path / "config.toml"
    config_file.write_text(toml_content)

    cfg = load_user_config(config_file)

    assert cfg.nxp_machine == "imx93-var-som"
    assert cfg.show_hashes is True
    # Everything else stays at the dataclass defaults.
    assert cfg.nxp_distro is None
    assert cfg.ti_machine is None
    assert cfg.container_image is None
    assert cfg.doctor is True


@pytest.mark.unit
def test_unknown_key_in_known_section_is_ignored(tmp_path: Path) -> None:
    toml_content = textwrap.dedent("""\
        [defaults.nxp]
        machine = "imx93-var-som"
        bogus_key = "ignored"

        [build]
        unknown = 42
        doctor = false
    """)
    config_file = tmp_path / "config.toml"
    config_file.write_text(toml_content)

    cfg = load_user_config(config_file)

    assert cfg.nxp_machine == "imx93-var-som"
    assert cfg.doctor is False
    assert not hasattr(cfg, "bogus_key")
    assert not hasattr(cfg, "unknown")


@pytest.mark.unit
def test_invalid_toml_raises_valueerror_with_path(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text("not valid toml [[[[\n")

    with pytest.raises(ValueError, match=re.escape(str(config_file))):
        load_user_config(config_file)


@pytest.mark.unit
def test_type_mismatch_raises_valueerror_with_path(tmp_path: Path) -> None:
    toml_content = textwrap.dedent("""\
        [defaults.nxp]
        machine = 123
    """)
    config_file = tmp_path / "config.toml"
    config_file.write_text(toml_content)

    with pytest.raises(ValueError, match=re.escape(str(config_file))):
        load_user_config(config_file)


@pytest.mark.unit
def test_build_tuning_keys_valid_types(tmp_path: Path) -> None:
    toml_content = textwrap.dedent("""\
        [build]
        dl_dir           = "/data/dl"
        sstate_dir       = "/data/sstate"
        sstate_mirrors   = "file:///mirror/sstate PATH"
        scheduler        = "completion"
        pressure_max_cpu = 60
        pressure_max_io  = 45
        pressure_max_memory = 20
    """)
    config_file = tmp_path / "config.toml"
    config_file.write_text(toml_content)

    cfg = load_user_config(config_file)

    assert cfg.dl_dir == "/data/dl"
    assert isinstance(cfg.dl_dir, str)
    assert cfg.sstate_dir == "/data/sstate"
    assert isinstance(cfg.sstate_dir, str)
    assert cfg.sstate_mirrors == "file:///mirror/sstate PATH"
    assert isinstance(cfg.sstate_mirrors, str)
    assert cfg.scheduler == "completion"
    assert isinstance(cfg.scheduler, str)
    assert cfg.pressure_max_cpu == 60
    assert isinstance(cfg.pressure_max_cpu, int)
    assert cfg.pressure_max_io == 45
    assert isinstance(cfg.pressure_max_io, int)
    assert cfg.pressure_max_memory == 20
    assert isinstance(cfg.pressure_max_memory, int)


@pytest.mark.unit
def test_build_tuning_keys_absent_yields_none_defaults(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text("[build]\ndoctor = true\n")

    cfg = load_user_config(config_file)

    assert cfg.dl_dir is None
    assert cfg.sstate_dir is None
    assert cfg.sstate_mirrors is None
    assert cfg.scheduler is None
    assert cfg.pressure_max_cpu is None
    assert cfg.pressure_max_io is None
    assert cfg.pressure_max_memory is None


@pytest.mark.unit
def test_pressure_key_string_value_raises_with_path(tmp_path: Path) -> None:
    toml_content = textwrap.dedent("""\
        [build]
        pressure_max_cpu = "high"
    """)
    config_file = tmp_path / "config.toml"
    config_file.write_text(toml_content)

    with pytest.raises(ValueError, match=re.escape(str(config_file))):
        load_user_config(config_file)


@pytest.mark.unit
def test_pressure_key_bool_value_raises_with_path(tmp_path: Path) -> None:
    toml_content = textwrap.dedent("""\
        [build]
        pressure_max_cpu = true
    """)
    config_file = tmp_path / "config.toml"
    config_file.write_text(toml_content)

    with pytest.raises(ValueError, match=re.escape(str(config_file))):
        load_user_config(config_file)


@pytest.mark.unit
def test_set_then_load_pressure_key_round_trip(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    set_setting("build.pressure_max_cpu", "55", path=config_file)

    cfg = load_user_config(config_file)

    assert cfg.pressure_max_cpu == 55
    assert isinstance(cfg.pressure_max_cpu, (int, float))


@pytest.mark.unit
def test_load_user_config_hashserv_default_false(tmp_path: Path) -> None:
    """`hashserv` defaults to False when the `[build]` table omits it."""
    config_file = tmp_path / "config.toml"
    config_file.write_text("[build]\ndoctor = true\n")

    cfg = load_user_config(config_file)

    assert cfg.hashserv is False
    assert isinstance(cfg.hashserv, bool)


@pytest.mark.unit
def test_load_user_config_hashserv_true_loads(tmp_path: Path) -> None:
    """`[build] hashserv = true` loads as a real boolean True."""
    config_file = tmp_path / "config.toml"
    config_file.write_text("[build]\nhashserv = true\n")

    cfg = load_user_config(config_file)

    assert cfg.hashserv is True
    assert isinstance(cfg.hashserv, bool)


@pytest.mark.unit
def test_load_user_config_hashserv_type_mismatch_raises(tmp_path: Path) -> None:
    """A non-bool value for `hashserv` raises ValueError mentioning the field."""
    toml_content = textwrap.dedent("""\
        [build]
        hashserv = "yes"
    """)
    config_file = tmp_path / "config.toml"
    config_file.write_text(toml_content)

    with pytest.raises(ValueError, match="hashserv"):
        load_user_config(config_file)


@pytest.mark.unit
def test_set_setting_build_hashserv_round_trip(tmp_path: Path) -> None:
    """`set_setting('build.hashserv', 'true')` round-trips through load_user_config."""
    config_file = tmp_path / "config.toml"
    set_setting("build.hashserv", "true", path=config_file)

    cfg = load_user_config(config_file)

    assert cfg.hashserv is True
    assert isinstance(cfg.hashserv, bool)
