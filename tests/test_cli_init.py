"""Unit tests for bakar.commands.init._scaffold_workspace.

Covers only the pure scaffold function - no questionary, no wizard prompts,
no mocking. Each family's directory layout and .bakar.toml content is verified
against a fresh tmp_path workspace.
"""

from __future__ import annotations

import tomllib
from typing import TYPE_CHECKING

import pytest

from bakar.commands.init import _scaffold_workspace

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


def _read_toml(workspace: Path) -> dict:
    with (workspace / ".bakar.toml").open("rb") as f:
        return tomllib.load(f)


@pytest.mark.unit
def test_nxp_creates_subdir_and_defaults_section(tmp_path: Path) -> None:
    settings = {
        "manifest": "imx-6.6.52-2.2.2.xml",
        "machine": "imx8mp-var-dart",
        "distro": "fsl-imx-xwayland",
        "image": "core-image-minimal",
    }
    _scaffold_workspace(tmp_path, "nxp", settings)

    assert (tmp_path / "nxp").is_dir()
    # nxp must not also create the ti/ subdir.
    assert not (tmp_path / "ti").exists()

    data = _read_toml(tmp_path)
    assert data["defaults"]["nxp"] == settings


@pytest.mark.unit
def test_ti_creates_subdir_and_defaults_section(tmp_path: Path) -> None:
    settings = {
        "manifest": "processor-sdk-scarthgap.txt",
        "machine": "am62x-var-som",
        "distro": "arago",
        "image": "var-thin-image",
    }
    _scaffold_workspace(tmp_path, "ti", settings)

    assert (tmp_path / "ti").is_dir()
    # ti must not also create the nxp/ subdir.
    assert not (tmp_path / "nxp").exists()

    data = _read_toml(tmp_path)
    assert data["defaults"]["ti"] == settings


@pytest.mark.unit
def test_bbsetup_is_comment_only_marker(tmp_path: Path) -> None:
    _scaffold_workspace(tmp_path, "bbsetup", {})

    # No family subdirectories.
    assert not (tmp_path / "nxp").exists()
    assert not (tmp_path / "ti").exists()

    marker = tmp_path / ".bakar.toml"
    assert marker.is_file()

    # Comment-only: parses to an empty table, no [defaults] section.
    data = _read_toml(tmp_path)
    assert data == {}
    assert "defaults" not in data


@pytest.mark.unit
def test_generic_writes_defaults_section_without_subdir(tmp_path: Path) -> None:
    settings = {
        "kas_yaml": "avocado-bspctl.yml",
        "machine": "qemux86-64",
    }
    _scaffold_workspace(tmp_path, "generic", settings)

    # No family subdirectories for generic.
    assert not (tmp_path / "nxp").exists()
    assert not (tmp_path / "ti").exists()

    data = _read_toml(tmp_path)
    assert data["defaults"]["generic"]["kas_yaml"] == "avocado-bspctl.yml"
    assert data["defaults"]["generic"]["machine"] == "qemux86-64"


@pytest.mark.unit
def test_second_call_raises_file_exists_error(tmp_path: Path) -> None:
    _scaffold_workspace(tmp_path, "generic", {"kas_yaml": "kas.yml", "machine": "qemux86-64"})

    # The builtin FileExistsError, not a custom subclass.
    with pytest.raises(FileExistsError) as excinfo:
        _scaffold_workspace(tmp_path, "generic", {"kas_yaml": "kas.yml", "machine": "qemux86-64"})

    assert type(excinfo.value) is FileExistsError
