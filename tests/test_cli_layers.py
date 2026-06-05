"""Tests for the ``bakar layers`` command.

Drives the command through the Typer ``CliRunner``, monkeypatching
``collect_layer_hashes`` so no real git work happens (mock pattern from
``tests/test_cli_user_config.py``). ``collect_layer_hashes`` is imported into
the command module, so it is patched on ``bakar.commands.layers`` - where the
``layers`` function looks it up.

Importing ``bakar.commands.layers`` registers the command on the shared
``app`` (cli.py does not yet import it; that wiring is task 5.1).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

import bakar.commands.layers as layers_module
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
    """A workspace with an ``nxp/`` subdir so workspace detection picks nxp."""
    (tmp_path / "nxp").mkdir()
    return tmp_path


@pytest.mark.unit
def test_populated_workspace_prints_layer_row(
    runner: _CliRunner, nxp_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-empty layer list exits 0 and prints a layer row."""
    sentinel = [LayerHash(repo="poky", short_hash="deadbee", branch="scarthgap")]
    monkeypatch.setattr(layers_module, "collect_layer_hashes", lambda cfg: sentinel)
    result = runner.invoke(app, ["layers", "--workspace", str(nxp_workspace)])
    assert result.exit_code == 0, result.output
    assert "layers (" in result.output
    assert "poky" in result.output
    assert "deadbee" in result.output


@pytest.mark.unit
def test_empty_workspace_prints_guidance_no_table(
    runner: _CliRunner, nxp_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty layer list exits 0, prints guidance, and prints no table."""
    monkeypatch.setattr(layers_module, "collect_layer_hashes", lambda cfg: [])
    result = runner.invoke(app, ["layers", "--workspace", str(nxp_workspace)])
    assert result.exit_code == 0, result.output
    assert "bakar build" in result.output
    assert "bakar sync" in result.output
    assert "layers:" not in result.output


@pytest.mark.unit
def test_yaml_and_manifest_mutually_exclusive(
    runner: _CliRunner, nxp_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Passing both a positional kas YAML and ``--manifest`` exits non-zero."""
    monkeypatch.setattr(layers_module, "collect_layer_hashes", lambda cfg: [])
    result = runner.invoke(
        app,
        [
            "layers",
            "my.yml",
            "--manifest",
            "imx-6.12.49-2.2.0.xml",
            "--workspace",
            str(nxp_workspace),
        ],
    )
    assert result.exit_code != 0, result.output


@pytest.mark.unit
def test_outside_workspace_fails(runner: _CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No positional YAML and no ``--workspace`` outside a workspace fails."""
    monkeypatch.setattr(layers_module, "collect_layer_hashes", lambda cfg: [])
    # tmp_path carries no .bakar.toml, nxp/, ti/, or bitbake-setup signature,
    # so _workspace_from_cwd raises typer.Exit(2).
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["layers"])
    assert result.exit_code != 0, result.output
    assert "workspace" in result.output.lower()
