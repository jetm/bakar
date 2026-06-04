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
