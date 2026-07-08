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
from bakar.commands.layers import (
    _check_compat_mismatch,
    _check_duplicate_priority,
    _check_orphan_bbappend,
)
from bakar.layers import LayerHash

if TYPE_CHECKING:
    from pathlib import Path

    from typer.testing import CliRunner as _CliRunner

pytestmark = pytest.mark.unit


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
    assert "Layers (" in result.output
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
    assert "Layers (" not in result.output


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


# ---------------------------------------------------------------------------
# Unit tests for the three cross-validation helper functions
# ---------------------------------------------------------------------------

# --- _check_compat_mismatch -------------------------------------------------


@pytest.mark.unit
def test_compat_mismatch_empty_codename_returns_empty() -> None:
    """Graceful skip when distro_codename is empty."""
    records = [{"name": "meta-foo", "compat": "dunfell"}]
    assert _check_compat_mismatch(records, "") == []


@pytest.mark.unit
def test_compat_mismatch_detected() -> None:
    """Layer with 'dunfell' compat and active codename 'scarthgap' produces a warning."""
    records = [{"name": "meta-foo", "compat": "dunfell"}]
    result = _check_compat_mismatch(records, "scarthgap")
    assert len(result) == 1
    assert "meta-foo" in result[0]
    assert "scarthgap" in result[0]


@pytest.mark.unit
def test_compat_mismatch_no_warning_when_codename_included() -> None:
    """Layer with 'dunfell scarthgap' compat and active 'scarthgap' is fine."""
    records = [{"name": "meta-bar", "compat": "dunfell scarthgap"}]
    assert _check_compat_mismatch(records, "scarthgap") == []


@pytest.mark.unit
def test_compat_mismatch_empty_compat_skipped() -> None:
    """Layers with empty compat are skipped - no warning emitted."""
    records = [{"name": "meta-baz", "compat": ""}]
    assert _check_compat_mismatch(records, "scarthgap") == []


@pytest.mark.unit
def test_compat_mismatch_absent_compat_skipped() -> None:
    """Layers missing the 'compat' key entirely are skipped."""
    records = [{"name": "meta-qux"}]
    assert _check_compat_mismatch(records, "scarthgap") == []


@pytest.mark.unit
def test_compat_mismatch_multiple_layers_partial_warning() -> None:
    """Only layers whose compat excludes the active codename are flagged."""
    records = [
        {"name": "meta-old", "compat": "dunfell"},
        {"name": "meta-new", "compat": "scarthgap"},
        {"name": "meta-both", "compat": "dunfell scarthgap"},
    ]
    result = _check_compat_mismatch(records, "scarthgap")
    assert len(result) == 1
    assert "meta-old" in result[0]


# --- _check_duplicate_priority ----------------------------------------------


@pytest.mark.unit
def test_duplicate_priority_detected() -> None:
    """Two layers sharing priority '10' produce one warning."""
    records = [
        {"name": "meta-foo", "priority": "10"},
        {"name": "meta-bar", "priority": "10"},
    ]
    result = _check_duplicate_priority(records)
    assert len(result) == 1
    assert "meta-foo" in result[0]
    assert "meta-bar" in result[0]
    assert "10" in result[0]


@pytest.mark.unit
def test_duplicate_priority_unique_returns_empty() -> None:
    """All unique priorities produce no warnings."""
    records = [
        {"name": "meta-foo", "priority": "5"},
        {"name": "meta-bar", "priority": "10"},
        {"name": "meta-baz", "priority": "15"},
    ]
    assert _check_duplicate_priority(records) == []


@pytest.mark.unit
def test_duplicate_priority_empty_priority_skipped() -> None:
    """Records with empty or absent priority are skipped entirely."""
    records = [
        {"name": "meta-foo", "priority": ""},
        {"name": "meta-bar"},
        {"name": "meta-baz", "priority": ""},
    ]
    assert _check_duplicate_priority(records) == []


@pytest.mark.unit
def test_duplicate_priority_non_numeric_skipped() -> None:
    """Records with non-numeric priority strings are skipped."""
    records = [
        {"name": "meta-foo", "priority": "high"},
        {"name": "meta-bar", "priority": "high"},
    ]
    assert _check_duplicate_priority(records) == []


@pytest.mark.unit
def test_duplicate_priority_three_layers_same_priority() -> None:
    """Three layers with the same priority produce a single combined warning."""
    records = [
        {"name": "meta-a", "priority": "6"},
        {"name": "meta-b", "priority": "6"},
        {"name": "meta-c", "priority": "6"},
    ]
    result = _check_duplicate_priority(records)
    assert len(result) == 1
    assert "meta-a" in result[0]
    assert "meta-b" in result[0]
    assert "meta-c" in result[0]


# --- _check_orphan_bbappend -------------------------------------------------


@pytest.mark.unit
def test_orphan_bbappend_detected(tmp_path: Path) -> None:
    """A .bbappend with no matching .bb in any active layer produces a warning."""
    layer = tmp_path / "meta-foo"
    layer.mkdir()
    (layer / "bar_1.0.bbappend").touch()

    result = _check_orphan_bbappend([("meta-foo", layer)])
    assert len(result) == 1
    assert "bar_1.0.bbappend" in result[0]


@pytest.mark.unit
def test_orphan_bbappend_glob_pattern_matched(tmp_path: Path) -> None:
    """A bar_%.bbappend is NOT orphan when bar_1.0.bb exists in another layer."""
    meta_foo = tmp_path / "meta-foo"
    meta_foo.mkdir()
    (meta_foo / "bar_%.bbappend").touch()

    meta_bar = tmp_path / "meta-bar"
    meta_bar.mkdir()
    (meta_bar / "bar_1.0.bb").touch()

    result = _check_orphan_bbappend([("meta-foo", meta_foo), ("meta-bar", meta_bar)])
    assert result == []


@pytest.mark.unit
def test_orphan_bbappend_empty_layer_list_returns_empty() -> None:
    """An empty layer list returns no warnings."""
    assert _check_orphan_bbappend([]) == []


@pytest.mark.unit
def test_orphan_bbappend_same_layer_provides_base_recipe(tmp_path: Path) -> None:
    """A .bbappend matched by a .bb in the same layer is not flagged as orphan."""
    layer = tmp_path / "meta-foo"
    layer.mkdir()
    (layer / "baz_2.0.bbappend").touch()
    (layer / "baz_2.0.bb").touch()

    result = _check_orphan_bbappend([("meta-foo", layer)])
    assert result == []


@pytest.mark.unit
def test_orphan_bbappend_no_bbappend_files_returns_empty(tmp_path: Path) -> None:
    """A layer with only .bb files and no .bbappend files returns no warnings."""
    layer = tmp_path / "meta-foo"
    layer.mkdir()
    (layer / "mypkg_1.0.bb").touch()

    result = _check_orphan_bbappend([("meta-foo", layer)])
    assert result == []
