"""Unit tests for bakar.vendor_config."""

from __future__ import annotations

import textwrap
from typing import TYPE_CHECKING

import pytest

from bakar.vendor_config import VendorEntry, load_vendors

if TYPE_CHECKING:
    from pathlib import Path

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
        container_image="example/kas:latest",
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
