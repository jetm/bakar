"""Unit tests for bakar.commands.init._scaffold_workspace and init CLI flags.

Covers the pure scaffold function (no questionary, no wizard prompts, no mocking)
and the non-interactive CLI flags added to enable scripted workspace creation.
"""

from __future__ import annotations

import tomllib
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from bakar.commands._app import app
from bakar.commands.init import _scaffold_workspace

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit

runner = CliRunner()


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


# ---------------------------------------------------------------------------
# Non-interactive CLI flag tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_init_noninteractive_nxp_creates_workspace(tmp_path: Path) -> None:
    result = runner.invoke(app, ["init", "--family", "nxp", "--workspace", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "nxp").is_dir()
    data = _read_toml(tmp_path)
    assert "nxp" in data["defaults"]


@pytest.mark.unit
def test_init_noninteractive_nxp_custom_settings(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "init",
            "--family",
            "nxp",
            "--workspace",
            str(tmp_path),
            "--manifest",
            "imx-6.6.52-2.2.2.xml",
            "--machine",
            "imx8mp-var-dart",
            "--distro",
            "fsl-imx-xwayland",
            "--image",
            "core-image-minimal",
        ],
    )
    assert result.exit_code == 0, result.output
    data = _read_toml(tmp_path)
    assert data["defaults"]["nxp"]["manifest"] == "imx-6.6.52-2.2.2.xml"
    assert data["defaults"]["nxp"]["machine"] == "imx8mp-var-dart"


@pytest.mark.unit
def test_init_noninteractive_generic_creates_workspace(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "init",
            "--family",
            "generic",
            "--workspace",
            str(tmp_path),
            "--kas-yaml",
            "avocado-bspctl.yml",
            "--machine",
            "qemux86-64",
        ],
    )
    assert result.exit_code == 0, result.output
    data = _read_toml(tmp_path)
    assert data["defaults"]["generic"]["kas_yaml"] == "avocado-bspctl.yml"
    assert data["defaults"]["generic"]["machine"] == "qemux86-64"


@pytest.mark.unit
def test_init_noninteractive_generic_defaults(tmp_path: Path) -> None:
    result = runner.invoke(app, ["init", "--family", "generic", "--workspace", str(tmp_path)])
    assert result.exit_code == 0, result.output
    data = _read_toml(tmp_path)
    assert data["defaults"]["generic"]["kas_yaml"] == "kas-generic.yml"
    assert data["defaults"]["generic"]["machine"] == "qemux86-64"


@pytest.mark.unit
def test_init_noninteractive_bbsetup_creates_marker(tmp_path: Path) -> None:
    result = runner.invoke(app, ["init", "--family", "bbsetup", "--workspace", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert (tmp_path / ".bakar.toml").is_file()
    assert not (tmp_path / "nxp").exists()


@pytest.mark.unit
def test_init_noninteractive_invalid_family_exits_1(tmp_path: Path) -> None:
    result = runner.invoke(app, ["init", "--family", "bogus", "--workspace", str(tmp_path)])
    assert result.exit_code == 1


@pytest.mark.unit
def test_init_noninteractive_already_initialized_exits_1(tmp_path: Path) -> None:
    runner.invoke(app, ["init", "--family", "generic", "--workspace", str(tmp_path)])
    result = runner.invoke(app, ["init", "--family", "generic", "--workspace", str(tmp_path)])
    assert result.exit_code == 1


@pytest.mark.unit
def test_init_interactive_mode_exits_on_non_tty() -> None:
    # CliRunner's stdin is not a TTY; init without --family hits the isatty() guard.
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 1
    assert "requires an interactive terminal" in result.output
