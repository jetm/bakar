"""Unit tests for bakar.vendor_config."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from bakar.vendor_config import VendorEntry, load_vendor_presets, load_vendors

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# VendorEntry validation
# ---------------------------------------------------------------------------


def test_valid_minimal_entry() -> None:
    entry = VendorEntry(name="acme", family="nxp", manifest_regex=r"imx-.*\.xml")
    assert entry.name == "acme"
    assert entry.family == "nxp"
    assert entry.manifest_regex == r"imx-.*\.xml"
    # All optional fields default to None
    assert entry.repo_url is None
    assert entry.default_machine is None


def test_valid_full_entry() -> None:
    entry = VendorEntry(
        name="acme-ti",
        family="ti",
        manifest_regex=r"am62x-.*\.xml",
        repo_url="https://github.com/example/bsp",
        kas_container_image="example/kas:latest",
        default_machine="am62x-var-som",
        default_distro="arago",
        default_image="arago-base-tisdk-image",
        default_manifest="ti-11.00.09.04.xml",
        default_branch="main",
        branch_by_manifest_prefix={"ti-11": "main"},
        tuning_overlay="meta-ti-overrides",
    )
    assert entry.family == "ti"
    assert entry.default_machine == "am62x-var-som"
    assert entry.branch_by_manifest_prefix == {"ti-11": "main"}


@pytest.mark.parametrize("family", ["nxp", "ti", "generic", "bbsetup"])
def test_family_valid(family: str) -> None:
    entry = VendorEntry(name="acme", family=family, manifest_regex=r".*\.xml")
    assert entry.family == family


def test_family_invalid() -> None:
    with pytest.raises(ValueError, match="family must be one of"):
        VendorEntry(name="bad", family="rockchip", manifest_regex=r"rk-.*\.xml")


def test_family_invalid_message_lists_all_families() -> None:
    with pytest.raises(ValueError) as exc_info:
        VendorEntry(name="bad", family="rockchip", manifest_regex=r"rk-.*\.xml")
    message = str(exc_info.value)
    for family in ("nxp", "ti", "generic", "bbsetup"):
        assert family in message


def test_regex_invalid() -> None:
    with pytest.raises(ValueError, match="not a valid regular expression"):
        VendorEntry(name="bad", family="nxp", manifest_regex="[invalid")


def test_regex_length_cap() -> None:
    long_regex = "a" * 201
    with pytest.raises(ValueError, match="manifest_regex exceeds"):
        VendorEntry(name="bad", family="nxp", manifest_regex=long_regex)


# ---------------------------------------------------------------------------
# load_vendors - file-based path
# ---------------------------------------------------------------------------


def test_load_vendors_missing_file(tmp_path: Path) -> None:
    result = load_vendors(tmp_path / "nonexistent.toml")
    assert result == []


def test_load_vendors_valid(tmp_path: Path) -> None:
    toml_content = textwrap.dedent("""\
        [[vendors]]
        name = "nxp-variscite"
        family = "nxp"
        manifest_regex = "imx-.*\\\\.xml"
        default_machine = "imx93-var-som"
    """)
    config_file = tmp_path / "vendors.toml"
    config_file.write_text(toml_content)

    entries = load_vendors(config_file)
    assert len(entries) == 1
    assert entries[0].name == "nxp-variscite"
    assert entries[0].family == "nxp"
    assert entries[0].default_machine == "imx93-var-som"


def test_load_vendors_legacy_container_image_key_accepted(tmp_path: Path) -> None:
    toml_content = textwrap.dedent("""\
        [[vendors]]
        name = "acme"
        family = "nxp"
        manifest_regex = "imx-.*\\\\.xml"
        container_image = "legacy/kas:4.0"
    """)
    config_file = tmp_path / "vendors.toml"
    config_file.write_text(toml_content)

    entries = load_vendors(config_file)
    assert len(entries) == 1
    assert entries[0].kas_container_image == "legacy/kas:4.0"


def test_load_vendors_invalid_family_raises(tmp_path: Path) -> None:
    toml_content = textwrap.dedent("""\
        [[vendors]]
        name = "bad-vendor"
        family = "rockchip"
        manifest_regex = "rk-.*\\\\.xml"
    """)
    config_file = tmp_path / "vendors.toml"
    config_file.write_text(toml_content)

    with pytest.raises(ValueError, match="family must be one of"):
        load_vendors(config_file)


def test_load_vendors_invalid_toml_syntax_raises_value_error_naming_path(tmp_path: Path) -> None:
    """Malformed TOML syntax raises ValueError naming the file path, not a raw TOMLDecodeError."""
    config_file = tmp_path / "vendors.toml"
    config_file.write_text("[[vendors]\nname = 'unterminated\n")

    with pytest.raises(ValueError, match="invalid TOML") as exc_info:
        load_vendors(config_file)
    assert str(config_file) in str(exc_info.value)


def test_load_vendors_malformed_entry_raises_value_error_not_type_error(tmp_path: Path) -> None:
    """A vendor entry missing a required field raises the wrapped ValueError, never a bare TypeError."""
    toml_content = textwrap.dedent("""\
        [[vendors]]
        name = "incomplete-vendor"
        family = "nxp"
    """)
    config_file = tmp_path / "vendors.toml"
    config_file.write_text(toml_content)

    with pytest.raises(ValueError, match="invalid vendor entry") as exc_info:
        load_vendors(config_file)
    assert str(config_file) in str(exc_info.value)


def test_detect_bsp_family_degrades_when_load_vendors_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """detect_bsp_family keeps its documented 'never raises' contract when load_vendors raises.

    A malformed vendors.toml now surfaces as ValueError from load_vendors (see
    test_load_vendors_invalid_toml_syntax_raises_value_error_naming_path above); detect_bsp_family
    must catch it and fall back to the built-in NXP/TI regexes instead of propagating.
    """
    from bakar.bsp_model import detect_bsp_family

    def _raise() -> list:
        raise ValueError("vendors.toml: invalid TOML: bad syntax")

    monkeypatch.setattr("bakar.bsp_model.load_vendors", _raise)

    # Built-in NXP regex still matches despite the broken vendor loader.
    assert detect_bsp_family(Path("imx-6.6.52-2.2.2.xml")) == "nxp"
    # Built-in TI regex still matches too.
    assert detect_bsp_family(Path("arago-console-image.txt")) == "ti"
    # No match anywhere degrades to "unknown", not an uncaught exception.
    assert detect_bsp_family(Path("garbage.xml")) == "unknown"


# ---------------------------------------------------------------------------
# load_vendor_presets
# ---------------------------------------------------------------------------


def test_load_vendor_presets_missing_file(tmp_path: Path) -> None:
    result = load_vendor_presets(tmp_path / "nonexistent.toml")
    assert result == []


def test_load_vendor_presets_no_presets_section(tmp_path: Path) -> None:
    config_file = tmp_path / "vendors.toml"
    config_file.write_text('[[vendors]]\nname = "acme"\nfamily = "nxp"\nmanifest_regex = "imx-.*\\\\.xml"\n')
    result = load_vendor_presets(config_file)
    assert result == []


def test_load_vendor_presets_returns_raw_dicts(tmp_path: Path) -> None:
    toml_content = textwrap.dedent("""\
        [[presets]]
        name = "imx8mp-scarthgap"
        family = "nxp"
        manifest = "imx-6.6.52-2.2.2.xml"
        branch = "lf-6.6.y"
        machine = "imx8mp-var-dart"
    """)
    config_file = tmp_path / "vendors.toml"
    config_file.write_text(toml_content)
    result = load_vendor_presets(config_file)
    assert len(result) == 1
    assert isinstance(result[0], dict)
    assert result[0]["name"] == "imx8mp-scarthgap"
    assert result[0]["family"] == "nxp"
    assert result[0]["manifest"] == "imx-6.6.52-2.2.2.xml"


def test_load_vendor_presets_multiple_entries(tmp_path: Path) -> None:
    toml_content = textwrap.dedent("""\
        [[presets]]
        name = "preset-a"
        family = "nxp"
        manifest = "imx-6.6.52-2.2.2.xml"
        branch = "lf-6.6.y"

        [[presets]]
        name = "preset-b"
        family = "bbsetup"
        kas_yaml = "conf/qemux86-64.yml"
    """)
    config_file = tmp_path / "vendors.toml"
    config_file.write_text(toml_content)
    result = load_vendor_presets(config_file)
    assert len(result) == 2
    names = {d["name"] for d in result}
    assert names == {"preset-a", "preset-b"}


def test_load_vendor_presets_default_path_missing(monkeypatch, tmp_path: Path) -> None:
    """When no path given and default file is absent, return []."""
    monkeypatch.setenv("HOME", str(tmp_path))
    result = load_vendor_presets()
    assert result == []
