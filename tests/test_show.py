"""Tests for the ``bakar show`` command.

Drives the command through the Typer ``CliRunner``, monkeypatching
``collect_layer_hashes``, ``discover_source_repos``, and ``_tuning_extra_overlays``
so no real git work or filesystem access happens.

Importing ``bakar.commands.show`` registers the command on the shared ``app``
(cli.py task 7.1 wires it; this module does the same import to make the test
self-contained).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

import bakar.commands.show as show_module
from bakar.cli import app
from bakar.layers import LayerHash

if TYPE_CHECKING:
    from pathlib import Path

    from typer.testing import CliRunner as _CliRunner

pytestmark = pytest.mark.unit


@pytest.fixture
def runner() -> _CliRunner:
    from typer.testing import CliRunner

    return CliRunner()


@pytest.fixture
def nxp_workspace(tmp_path: Path) -> Path:
    """A minimal NXP workspace with a ``.bakar.toml`` marker."""
    (tmp_path / ".bakar.toml").write_text("")
    (tmp_path / "nxp").mkdir()
    return tmp_path


@pytest.fixture
def sample_layer_hashes() -> list[LayerHash]:
    return [
        LayerHash(repo="meta-imx", short_hash="abc1234", branch="scarthgap"),
        LayerHash(repo="poky", short_hash="def5678", branch="scarthgap"),
    ]


@pytest.fixture
def sample_sources(tmp_path: Path) -> list[tuple[str, Path]]:
    return [
        ("meta-imx", tmp_path / "nxp" / "sources" / "meta-imx"),
        ("poky", tmp_path / "nxp" / "sources" / "poky"),
    ]


# ---------------------------------------------------------------------------
# Text output
# ---------------------------------------------------------------------------


def test_show_text_sections_present(
    runner: _CliRunner,
    nxp_workspace: Path,
    sample_layer_hashes: list[LayerHash],
    sample_sources: list[tuple[str, Path]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Text output contains all five section headers."""
    monkeypatch.setattr(show_module, "collect_layer_hashes", lambda cfg: sample_layer_hashes)
    monkeypatch.setattr(show_module, "discover_source_repos", lambda cfg: sample_sources)
    monkeypatch.setattr(show_module, "_tuning_extra_overlays", lambda cfg: [])

    result = runner.invoke(app, ["show", "--workspace", str(nxp_workspace)])

    assert result.exit_code == 0, result.output
    assert "Config:" in result.output
    assert "Overlays:" in result.output
    assert "Layers:" in result.output
    assert "Sources:" in result.output
    assert "Command:" in result.output


def test_show_text_config_fields(
    runner: _CliRunner,
    nxp_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Config section contains machine, distro, image, bsp_family."""
    monkeypatch.setattr(show_module, "collect_layer_hashes", lambda cfg: [])
    monkeypatch.setattr(show_module, "discover_source_repos", lambda cfg: [])
    monkeypatch.setattr(show_module, "_tuning_extra_overlays", lambda cfg: [])

    result = runner.invoke(app, ["show", "--workspace", str(nxp_workspace)])

    assert result.exit_code == 0, result.output
    assert "machine:" in result.output
    assert "distro:" in result.output
    assert "image:" in result.output
    assert "bsp_family:" in result.output


def test_show_text_layer_rows(
    runner: _CliRunner,
    nxp_workspace: Path,
    sample_layer_hashes: list[LayerHash],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Layers section shows each layer's repo name and short hash."""
    monkeypatch.setattr(show_module, "collect_layer_hashes", lambda cfg: sample_layer_hashes)
    monkeypatch.setattr(show_module, "discover_source_repos", lambda cfg: [])
    monkeypatch.setattr(show_module, "_tuning_extra_overlays", lambda cfg: [])

    result = runner.invoke(app, ["show", "--workspace", str(nxp_workspace)])

    assert result.exit_code == 0, result.output
    assert "meta-imx" in result.output
    assert "abc1234" in result.output
    assert "poky" in result.output
    assert "def5678" in result.output


def test_show_text_sources_rows(
    runner: _CliRunner,
    nxp_workspace: Path,
    sample_sources: list[tuple[str, Path]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sources section shows each source repo name."""
    monkeypatch.setattr(show_module, "collect_layer_hashes", lambda cfg: [])
    monkeypatch.setattr(show_module, "discover_source_repos", lambda cfg: sample_sources)
    monkeypatch.setattr(show_module, "_tuning_extra_overlays", lambda cfg: [])

    result = runner.invoke(app, ["show", "--workspace", str(nxp_workspace)])

    assert result.exit_code == 0, result.output
    assert "meta-imx" in result.output
    assert "poky" in result.output


def test_show_empty_workspace_exits_0(
    runner: _CliRunner,
    nxp_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An un-built workspace (no layers, no sources) still exits 0."""
    monkeypatch.setattr(show_module, "collect_layer_hashes", lambda cfg: [])
    monkeypatch.setattr(show_module, "discover_source_repos", lambda cfg: [])
    monkeypatch.setattr(show_module, "_tuning_extra_overlays", lambda cfg: [])

    result = runner.invoke(app, ["show", "--workspace", str(nxp_workspace)])

    assert result.exit_code == 0, result.output
    # Config section still present
    assert "Config:" in result.output
    # Command section still present
    assert "Command:" in result.output


# ---------------------------------------------------------------------------
# JSON output
# ---------------------------------------------------------------------------


def test_show_json_valid_and_keys_present(
    runner: _CliRunner,
    nxp_workspace: Path,
    sample_layer_hashes: list[LayerHash],
    sample_sources: list[tuple[str, Path]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--json`` output parses as JSON and carries the required top-level keys."""
    monkeypatch.setattr(show_module, "collect_layer_hashes", lambda cfg: sample_layer_hashes)
    monkeypatch.setattr(show_module, "discover_source_repos", lambda cfg: sample_sources)
    monkeypatch.setattr(show_module, "_tuning_extra_overlays", lambda cfg: [])

    result = runner.invoke(app, ["show", "--workspace", str(nxp_workspace), "--json"])

    assert result.exit_code == 0, result.output
    doc = json.loads(result.output)
    assert "config" in doc
    assert "overlays" in doc
    assert "layers" in doc
    assert "sources" in doc
    assert "command" in doc


def test_show_json_config_subkeys(
    runner: _CliRunner,
    nxp_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``config`` key in JSON output contains the expected subkeys."""
    monkeypatch.setattr(show_module, "collect_layer_hashes", lambda cfg: [])
    monkeypatch.setattr(show_module, "discover_source_repos", lambda cfg: [])
    monkeypatch.setattr(show_module, "_tuning_extra_overlays", lambda cfg: [])

    result = runner.invoke(app, ["show", "--workspace", str(nxp_workspace), "--json"])

    assert result.exit_code == 0, result.output
    doc = json.loads(result.output)
    cfg = doc["config"]
    assert "machine" in cfg
    assert "distro" in cfg
    assert "image" in cfg
    assert "bsp_family" in cfg
    assert "container_image" in cfg


def test_show_json_layers_populated(
    runner: _CliRunner,
    nxp_workspace: Path,
    sample_layer_hashes: list[LayerHash],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``layers`` JSON key lists all layer entries when present."""
    monkeypatch.setattr(show_module, "collect_layer_hashes", lambda cfg: sample_layer_hashes)
    monkeypatch.setattr(show_module, "discover_source_repos", lambda cfg: [])
    monkeypatch.setattr(show_module, "_tuning_extra_overlays", lambda cfg: [])

    result = runner.invoke(app, ["show", "--workspace", str(nxp_workspace), "--json"])

    assert result.exit_code == 0, result.output
    doc = json.loads(result.output)
    assert len(doc["layers"]) == 2
    repos = [entry["repo"] for entry in doc["layers"]]
    assert "meta-imx" in repos
    assert "poky" in repos


def test_show_json_command_is_string(
    runner: _CliRunner,
    nxp_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``command`` JSON key is a non-empty string."""
    monkeypatch.setattr(show_module, "collect_layer_hashes", lambda cfg: [])
    monkeypatch.setattr(show_module, "discover_source_repos", lambda cfg: [])
    monkeypatch.setattr(show_module, "_tuning_extra_overlays", lambda cfg: [])

    result = runner.invoke(app, ["show", "--workspace", str(nxp_workspace), "--json"])

    assert result.exit_code == 0, result.output
    doc = json.loads(result.output)
    assert isinstance(doc["command"], str)
    assert len(doc["command"]) > 0


# ---------------------------------------------------------------------------
# Markdown format
# ---------------------------------------------------------------------------


def test_show_format_md_section_headings(
    runner: _CliRunner,
    nxp_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--format md`` uses ``## Heading`` style for each section."""
    monkeypatch.setattr(show_module, "collect_layer_hashes", lambda cfg: [])
    monkeypatch.setattr(show_module, "discover_source_repos", lambda cfg: [])
    monkeypatch.setattr(show_module, "_tuning_extra_overlays", lambda cfg: [])

    result = runner.invoke(app, ["show", "--workspace", str(nxp_workspace), "--format", "md"])

    assert result.exit_code == 0, result.output
    assert "## Config" in result.output
    assert "## Overlays" in result.output
    assert "## Layers" in result.output
    assert "## Sources" in result.output
    assert "## Command" in result.output


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_show_no_workspace_exits_2(
    runner: _CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Running from a directory with no resolvable workspace exits 2."""
    # tmp_path has no .bakar.toml, nxp/, ti/, or bitbake-setup signature.
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["show"])

    assert result.exit_code == 2, result.output
