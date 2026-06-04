"""Tests for `bakar presets` sub-app."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from bakar.commands._app import app

pytestmark = pytest.mark.unit

runner = CliRunner()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_config(tmp_path: Path, presets: list[dict]) -> Path:
    """Write a minimal config.toml with the given presets array."""
    import tomli_w

    config = {"presets": presets}
    path = tmp_path / "config.toml"
    path.write_bytes(tomli_w.dumps(config))
    return path


# ---------------------------------------------------------------------------
# list verb
# ---------------------------------------------------------------------------


def test_list_no_presets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """list with no presets defined prints a message and exits 0."""
    empty_config = tmp_path / "config.toml"
    empty_config.write_text("")
    empty_vendors = tmp_path / "vendors.toml"
    empty_vendors.write_text("")

    import bakar.commands.presets as presets_mod

    monkeypatch.setattr(
        presets_mod,
        "load_presets",
        list,
    )

    result = runner.invoke(app, ["presets", "list"])
    assert result.exit_code == 0
    assert "No presets defined." in result.output


def test_list_shows_name_and_family(monkeypatch: pytest.MonkeyPatch) -> None:
    """list with two presets shows one row per preset with name and family."""
    import bakar.commands.presets as presets_mod
    from bakar.preset_config import PresetEntry

    preset1 = PresetEntry(name="imx8mp-scarthgap", family="nxp", manifest="imx-6.6.52-2.2.2.xml", branch="lf-6.6.y")
    preset2 = PresetEntry(name="qemu-kirkstone", family="bbsetup", kas_yaml="layers/qemu.yml")

    monkeypatch.setattr(
        presets_mod,
        "load_presets",
        lambda: [preset1, preset2],
    )

    result = runner.invoke(app, ["presets", "list"])
    assert result.exit_code == 0
    assert "imx8mp-scarthgap" in result.output
    assert "nxp" in result.output
    assert "qemu-kirkstone" in result.output
    assert "bbsetup" in result.output


# ---------------------------------------------------------------------------
# show verb
# ---------------------------------------------------------------------------


def test_show_unknown_exits_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    """show with an unknown preset name exits non-zero and names the preset."""
    import bakar.commands.presets as presets_mod

    monkeypatch.setattr(presets_mod, "load_presets", list)

    result = runner.invoke(app, ["presets", "show", "does-not-exist"])
    assert result.exit_code != 0
    assert "does-not-exist" in result.output


def test_show_single_release_nxp(monkeypatch: pytest.MonkeyPatch) -> None:
    """show prints family, machine, manifest, and branch for a single-release nxp preset."""
    import bakar.commands.presets as presets_mod
    from bakar.preset_config import PresetEntry

    preset = PresetEntry(
        name="imx8mp-scarthgap",
        family="nxp",
        machine="imx8mpevk",
        distro="fsl-imx-xwayland",
        image="imx-image-full",
        manifest="imx-6.6.52-2.2.2.xml",
        branch="lf-6.6.y",
    )
    monkeypatch.setattr(presets_mod, "load_presets", lambda: [preset])

    result = runner.invoke(app, ["presets", "show", "imx8mp-scarthgap"])
    assert result.exit_code == 0
    assert "nxp" in result.output
    assert "imx8mpevk" in result.output
    assert "imx-6.6.52-2.2.2.xml" in result.output
    assert "lf-6.6.y" in result.output
    assert "fsl-imx-xwayland" in result.output
    assert "imx-image-full" in result.output


def test_show_single_release_bbsetup(monkeypatch: pytest.MonkeyPatch) -> None:
    """show prints kas_yaml for a single-release bbsetup preset."""
    import bakar.commands.presets as presets_mod
    from bakar.preset_config import PresetEntry

    preset = PresetEntry(
        name="qemu-kirkstone",
        family="bbsetup",
        machine="qemux86-64",
        image="avocado-os",
        kas_yaml="layers/qemu-kirkstone.yml",
    )
    monkeypatch.setattr(presets_mod, "load_presets", lambda: [preset])

    result = runner.invoke(app, ["presets", "show", "qemu-kirkstone"])
    assert result.exit_code == 0
    assert "bbsetup" in result.output
    assert "qemux86-64" in result.output
    assert "layers/qemu-kirkstone.yml" in result.output


def test_show_multi_release_lists_each_release(monkeypatch: pytest.MonkeyPatch) -> None:
    """show for a multi-release preset lists each release separately."""
    import bakar.commands.presets as presets_mod
    from bakar.preset_config import PresetEntry

    preset = PresetEntry(
        name="avocado-all-releases",
        family="nxp",
        machine="imx8mpevk",
        manifests=["imx-6.1.36-2.1.0.xml", "imx-6.6.52-2.2.2.xml"],
        branches=["lf-6.1.y", "lf-6.6.y"],
    )
    monkeypatch.setattr(presets_mod, "load_presets", lambda: [preset])

    result = runner.invoke(app, ["presets", "show", "avocado-all-releases"])
    assert result.exit_code == 0
    assert "2" in result.output  # Releases count
    assert "imx-6.1.36-2.1.0.xml" in result.output
    assert "imx-6.6.52-2.2.2.xml" in result.output
    assert "lf-6.1.y" in result.output
    assert "lf-6.6.y" in result.output
