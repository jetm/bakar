"""Tests for `bakar presets` sub-app."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path
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
    with path.open("wb") as _f:
        tomli_w.dump(config, _f)
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


# ---------------------------------------------------------------------------
# add verb
# ---------------------------------------------------------------------------


def test_add_no_tty_exits_nonzero() -> None:
    """add without a TTY exits non-zero without blocking on a prompt."""
    # CliRunner uses a non-TTY stdin by default (mix_stderr=False, no isatty).
    result = runner.invoke(app, ["presets", "add"])
    assert result.exit_code != 0


def test_add_nxp_writes_presets_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """add with mocked questionary for nxp writes a [[presets]] entry to config.toml."""
    import bakar.commands.presets as presets_mod

    config_path = tmp_path / "config.toml"

    # Patch the config path used by add_preset.
    monkeypatch.setattr(presets_mod, "_CONFIG_PATH", config_path)

    # Bypass the TTY guard.
    monkeypatch.setattr(presets_mod, "_is_tty", lambda: True)

    # Mock questionary prompts in order: family, name, manifest, branch, machine, distro, image.
    answers = iter(
        ["nxp", "my-nxp-preset", "imx-6.6.52-2.2.2.xml", "lf-6.6.y", "imx8mpevk", "fsl-imx-xwayland", "imx-image-full"]
    )

    class _FakeQuestion:
        def __init__(self, answer: str) -> None:
            self._answer = answer

        def ask(self) -> str:
            return self._answer

    def _fake_select(msg: str, choices: list[str]) -> _FakeQuestion:
        return _FakeQuestion(next(answers))

    def _fake_text(msg: str, default: str = "") -> _FakeQuestion:
        return _FakeQuestion(next(answers))

    def _fake_path(msg: str, default: str = "") -> _FakeQuestion:
        return _FakeQuestion(next(answers))

    monkeypatch.setattr(presets_mod.questionary, "select", _fake_select)
    monkeypatch.setattr(presets_mod.questionary, "text", _fake_text)
    monkeypatch.setattr(presets_mod.questionary, "path", _fake_path)

    result = runner.invoke(app, ["presets", "add"])
    assert result.exit_code == 0, result.output

    # Verify config.toml was written and contains the new preset.
    import tomllib

    with config_path.open("rb") as f:
        data = tomllib.load(f)

    assert "presets" in data
    assert len(data["presets"]) == 1
    preset = data["presets"][0]
    assert preset["name"] == "my-nxp-preset"
    assert preset["family"] == "nxp"
    assert preset["manifest"] == "imx-6.6.52-2.2.2.xml"
    assert preset["branch"] == "lf-6.6.y"
    assert preset["machine"] == "imx8mpevk"
    assert preset["distro"] == "fsl-imx-xwayland"
    assert preset["image"] == "imx-image-full"


def test_add_bbsetup_prompts_kas_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """add with bbsetup family prompts for kas_yaml, machine, image - not manifest/branch."""
    import bakar.commands.presets as presets_mod

    config_path = tmp_path / "config.toml"
    monkeypatch.setattr(presets_mod, "_CONFIG_PATH", config_path)
    monkeypatch.setattr(presets_mod, "_is_tty", lambda: True)

    # Order: family, name, kas_yaml path, machine, image.
    answers = iter(["bbsetup", "my-bbsetup-preset", "layers/qemu.yml", "qemux86-64", "avocado-os"])

    class _FakeQuestion:
        def __init__(self, answer: str) -> None:
            self._answer = answer

        def ask(self) -> str:
            return self._answer

    def _fake_select(msg: str, choices: list[str]) -> _FakeQuestion:
        return _FakeQuestion(next(answers))

    def _fake_text(msg: str, default: str = "") -> _FakeQuestion:
        return _FakeQuestion(next(answers))

    def _fake_path(msg: str, default: str = "") -> _FakeQuestion:
        return _FakeQuestion(next(answers))

    monkeypatch.setattr(presets_mod.questionary, "select", _fake_select)
    monkeypatch.setattr(presets_mod.questionary, "text", _fake_text)
    monkeypatch.setattr(presets_mod.questionary, "path", _fake_path)

    result = runner.invoke(app, ["presets", "add"])
    assert result.exit_code == 0, result.output

    import tomllib

    with config_path.open("rb") as f:
        data = tomllib.load(f)

    preset = data["presets"][0]
    assert preset["family"] == "bbsetup"
    assert preset["kas_yaml"] == "layers/qemu.yml"
    assert preset["machine"] == "qemux86-64"
    assert preset["image"] == "avocado-os"
    # nxp-specific keys must not be present.
    assert "manifest" not in preset
    assert "branch" not in preset


def test_add_appends_to_existing_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """add appends a new preset to an already-populated config.toml."""
    import tomli_w

    import bakar.commands.presets as presets_mod

    config_path = tmp_path / "config.toml"
    # Write an existing preset.
    existing = {
        "presets": [
            {"name": "existing-preset", "family": "nxp", "manifest": "imx-6.1.36-2.1.0.xml", "branch": "lf-6.1.y"}
        ]
    }
    with config_path.open("wb") as _f:
        tomli_w.dump(existing, _f)

    monkeypatch.setattr(presets_mod, "_CONFIG_PATH", config_path)
    monkeypatch.setattr(presets_mod, "_is_tty", lambda: True)

    answers = iter(["generic", "second-preset", "kas-second.yml", "qemux86-64", "avocado-os"])

    class _FakeQuestion:
        def __init__(self, answer: str) -> None:
            self._answer = answer

        def ask(self) -> str:
            return self._answer

    monkeypatch.setattr(presets_mod.questionary, "select", lambda msg, choices: _FakeQuestion(next(answers)))
    monkeypatch.setattr(presets_mod.questionary, "text", lambda msg, default="": _FakeQuestion(next(answers)))
    monkeypatch.setattr(presets_mod.questionary, "path", lambda msg, default="": _FakeQuestion(next(answers)))

    result = runner.invoke(app, ["presets", "add"])
    assert result.exit_code == 0, result.output

    import tomllib

    with config_path.open("rb") as f:
        data = tomllib.load(f)

    assert len(data["presets"]) == 2
    names = {p["name"] for p in data["presets"]}
    assert "existing-preset" in names
    assert "second-preset" in names
