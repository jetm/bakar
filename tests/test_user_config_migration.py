"""Unit tests for versioned config-schema forward migration in bakar.user_config."""

from __future__ import annotations

import textwrap
import tomllib
from typing import TYPE_CHECKING

import pytest

from bakar.user_config import (
    CURRENT_CONFIG_VERSION,
    UserConfig,
    _migrate_config,
    load_user_config,
)

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


def _write(path: Path, content: str) -> Path:
    path.write_text(textwrap.dedent(content))
    return path


def test_missing_file_is_current_version(tmp_path: Path) -> None:
    cfg = load_user_config(tmp_path / "absent.toml")
    assert cfg == UserConfig()
    assert cfg.config_version == CURRENT_CONFIG_VERSION


def test_legacy_config_without_version_migrates_to_current(tmp_path: Path) -> None:
    config_file = _write(
        tmp_path / "config.toml",
        """\
        [defaults.nxp]
        machine = "imx8mp-var-dart"
        """,
    )

    cfg = load_user_config(config_file)

    assert cfg.nxp_machine == "imx8mp-var-dart"
    assert cfg.config_version == CURRENT_CONFIG_VERSION


def test_legacy_config_migration_persists_version_to_disk(tmp_path: Path) -> None:
    config_file = _write(
        tmp_path / "config.toml",
        """\
        [build]
        kas_container_image = "jetm/kas-build-env:latest"
        """,
    )

    load_user_config(config_file)

    with config_file.open("rb") as f:
        on_disk = tomllib.load(f)
    assert on_disk["config_version"] == CURRENT_CONFIG_VERSION
    # Pre-existing keys survive the rewrite.
    assert on_disk["build"]["kas_container_image"] == "jetm/kas-build-env:latest"


def test_v1_to_v2_migrates_doctor_false_to_show_doctor_report(tmp_path: Path) -> None:
    config_file = _write(
        tmp_path / "config.toml",
        """\
        config_version = 1

        [build]
        doctor = false
        """,
    )

    cfg = load_user_config(config_file)

    assert cfg.show_doctor_report is False
    assert cfg.config_version == CURRENT_CONFIG_VERSION
    with config_file.open("rb") as f:
        on_disk = tomllib.load(f)
    assert on_disk["build"]["show_doctor_report"] is False
    assert "doctor" not in on_disk["build"]


def test_v1_to_v2_drops_doctor_true_without_setting_show_doctor_report(tmp_path: Path) -> None:
    config_file = _write(
        tmp_path / "config.toml",
        """\
        config_version = 1

        [build]
        doctor = true
        """,
    )

    cfg = load_user_config(config_file)

    # doctor=true matched the default-on case; it just drops, leaving show_doctor_report
    # at its True default rather than writing a redundant key.
    assert cfg.show_doctor_report is True
    with config_file.open("rb") as f:
        on_disk = tomllib.load(f)
    assert "doctor" not in on_disk.get("build", {})
    assert "show_doctor_report" not in on_disk.get("build", {})


def test_current_version_loads_unchanged(tmp_path: Path) -> None:
    config_file = _write(
        tmp_path / "config.toml",
        f"""\
        config_version = {CURRENT_CONFIG_VERSION}

        [defaults.ti]
        machine = "am62x-var-som"
        """,
    )
    before = config_file.read_bytes()

    cfg = load_user_config(config_file)

    assert cfg.ti_machine == "am62x-var-som"
    assert cfg.config_version == CURRENT_CONFIG_VERSION
    # An already-current config is not rewritten.
    assert config_file.read_bytes() == before


def test_future_version_raises_naming_version(tmp_path: Path) -> None:
    future = CURRENT_CONFIG_VERSION + 7
    config_file = _write(
        tmp_path / "config.toml",
        f"""\
        config_version = {future}
        """,
    )

    with pytest.raises(ValueError, match=str(future)):
        load_user_config(config_file)


def test_future_version_does_not_rewrite_file(tmp_path: Path) -> None:
    config_file = _write(
        tmp_path / "config.toml",
        f"""\
        config_version = {CURRENT_CONFIG_VERSION + 1}
        """,
    )
    before = config_file.read_bytes()

    with pytest.raises(ValueError):
        load_user_config(config_file)

    assert config_file.read_bytes() == before


def test_non_integer_version_raises(tmp_path: Path) -> None:
    config_file = _write(
        tmp_path / "config.toml",
        """\
        config_version = "1"
        """,
    )

    with pytest.raises(ValueError, match="config_version"):
        load_user_config(config_file)


def test_bool_version_rejected(tmp_path: Path) -> None:
    config_file = _write(
        tmp_path / "config.toml",
        """\
        config_version = true
        """,
    )

    with pytest.raises(ValueError, match="config_version"):
        load_user_config(config_file)


def test_migrate_config_stamps_current_version_from_zero() -> None:
    raw: dict[str, object] = {"build": {"kas_container_image": "x"}}
    out = _migrate_config(raw, 0)
    assert out["config_version"] == CURRENT_CONFIG_VERSION
    assert out["build"] == {"kas_container_image": "x"}


def test_v2_to_v3_renames_container_image_to_kas_container_image() -> None:
    raw: dict[str, object] = {"build": {"container_image": "custom/kas:4.7"}}
    out = _migrate_config(raw, 2)
    assert out["config_version"] == CURRENT_CONFIG_VERSION
    assert out["build"] == {"kas_container_image": "custom/kas:4.7"}


def test_v2_to_v3_no_op_when_container_image_absent() -> None:
    raw: dict[str, object] = {"build": {"dl_dir": "/data/dl"}}
    out = _migrate_config(raw, 2)
    assert out["config_version"] == CURRENT_CONFIG_VERSION
    assert out["build"] == {"dl_dir": "/data/dl"}


def test_v2_to_v3_preserves_kas_container_image_when_both_keys_present() -> None:
    raw: dict[str, object] = {"build": {"container_image": "old/kas:1.0", "kas_container_image": "new/kas:2.0"}}
    out = _migrate_config(raw, 2)
    assert out["config_version"] == CURRENT_CONFIG_VERSION
    assert out["build"] == {"kas_container_image": "new/kas:2.0"}


def test_migrate_config_no_op_at_current_version() -> None:
    raw: dict[str, object] = {"config_version": CURRENT_CONFIG_VERSION}
    out = _migrate_config(raw, CURRENT_CONFIG_VERSION)
    assert out["config_version"] == CURRENT_CONFIG_VERSION
