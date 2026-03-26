"""Tests for _workspace_from_cwd() workspace detection logic.

Covers the six cases from spec workspace-detection:
1. marker-only: .bspctl.toml present, no nxp/ or ti/ -> detected via marker
2. subdir-only: nxp/ subdir present, no marker -> detected via subdir
3. both-present: both .bspctl.toml and nxp/ -> marker wins (same dir, either works)
4. neither: no marker, no nxp/, no ti/ -> exit 2
5. stray varis/ dir: only varis/ dir present -> NOT detected (ignored)
6. generic BYO carve-out: --workspace flag bypasses detection
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import typer

if TYPE_CHECKING:
    from pathlib import Path

from bspctl.cli import _resolve_workspace, _workspace_from_cwd

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _chdir(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    """Change the working directory for the duration of the test."""
    monkeypatch.chdir(path)


# ---------------------------------------------------------------------------
# Case 1: .bspctl.toml marker only
# ---------------------------------------------------------------------------


def test_marker_only_detected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A .bspctl.toml in the cwd is sufficient for detection."""
    (tmp_path / ".bspctl.toml").write_text("[bspctl]\n")
    _chdir(monkeypatch, tmp_path)

    result = _workspace_from_cwd()

    assert result == tmp_path.resolve()


def test_marker_in_parent_detected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Walking up finds .bspctl.toml in a parent directory."""
    (tmp_path / ".bspctl.toml").write_text("[bspctl]\n")
    nested = tmp_path / "nxp" / "build"
    nested.mkdir(parents=True)
    _chdir(monkeypatch, nested)

    result = _workspace_from_cwd()

    assert result == tmp_path.resolve()


# ---------------------------------------------------------------------------
# Case 2: nxp/ subdir only (no marker)
# ---------------------------------------------------------------------------


def test_subdir_nxp_detected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An nxp/ subdirectory triggers subdir detection."""
    (tmp_path / "nxp").mkdir()
    _chdir(monkeypatch, tmp_path)

    result = _workspace_from_cwd()

    assert result == tmp_path.resolve()


def test_subdir_ti_detected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A ti/ subdirectory triggers subdir detection."""
    (tmp_path / "ti").mkdir()
    _chdir(monkeypatch, tmp_path)

    result = _workspace_from_cwd()

    assert result == tmp_path.resolve()


# ---------------------------------------------------------------------------
# Case 3: both .bspctl.toml and nxp/ present -> marker wins
# ---------------------------------------------------------------------------


def test_marker_wins_over_subdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When both .bspctl.toml and nxp/ are present, marker path is taken.

    Because the marker check comes first in the loop body, the marker is
    what triggers detection, but the returned path is the same directory
    in either case. This test confirms both signals coexist without error
    and the correct workspace is returned.
    """
    (tmp_path / ".bspctl.toml").write_text("[bspctl]\n")
    (tmp_path / "nxp").mkdir()
    _chdir(monkeypatch, tmp_path)

    result = _workspace_from_cwd()

    assert result == tmp_path.resolve()


def test_marker_in_subdir_wins_over_parent_nxp_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Marker closer to cwd beats nxp/ further up the tree.

    Layout:
      tmp/           <- has nxp/
        nxp/
        inner/       <- has .bspctl.toml; cwd

    The walk starts at tmp/inner/ and finds .bspctl.toml there first,
    returning tmp/inner/ not tmp/.
    """
    (tmp_path / "nxp").mkdir()
    inner = tmp_path / "inner"
    inner.mkdir()
    (inner / ".bspctl.toml").write_text("[bspctl]\n")
    _chdir(monkeypatch, inner)

    result = _workspace_from_cwd()

    assert result == inner.resolve()


# ---------------------------------------------------------------------------
# Case 4: neither marker nor nxp/ nor ti/ -> exit 2
# ---------------------------------------------------------------------------


def test_neither_exits_2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When no marker and no nxp/ti/ subdir are found, exits with code 2."""
    _chdir(monkeypatch, tmp_path)

    with pytest.raises(typer.Exit) as exc_info:
        _workspace_from_cwd()

    assert exc_info.value.exit_code == 2


# ---------------------------------------------------------------------------
# Case 5: stray varis/ dir present -> NOT detected (ignored)
# ---------------------------------------------------------------------------


def test_stray_varis_dir_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A varis/ directory alone does not satisfy workspace detection.

    bspctl no longer looks for varis/ - it was removed when the tool was
    renamed. Only .bspctl.toml, nxp/, or ti/ are recognized.
    """
    (tmp_path / "varis").mkdir()
    _chdir(monkeypatch, tmp_path)

    with pytest.raises(typer.Exit) as exc_info:
        _workspace_from_cwd()

    assert exc_info.value.exit_code == 2


def test_stray_varis_plus_no_other_signal_still_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """varis/ combined with other non-signal dirs still fails detection."""
    for name in ("varis", "build", "src", "scripts"):
        (tmp_path / name).mkdir()
    _chdir(monkeypatch, tmp_path)

    with pytest.raises(typer.Exit) as exc_info:
        _workspace_from_cwd()

    assert exc_info.value.exit_code == 2


# ---------------------------------------------------------------------------
# Case 6: --workspace flag bypasses detection (generic BYO carve-out)
# ---------------------------------------------------------------------------


def test_workspace_flag_bypasses_cwd_walk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When workspace is provided explicitly, _workspace_from_cwd is not called.

    _resolve_workspace returns the supplied workspace directly, so even
    a directory with no detection signals is accepted.
    """
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    _chdir(monkeypatch, empty_dir)

    explicit_ws = tmp_path / "my-workspace"
    explicit_ws.mkdir()

    result = _resolve_workspace(explicit_ws)

    assert result == explicit_ws


def test_workspace_flag_accepts_any_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The --workspace path is returned as-is without validation."""
    _chdir(monkeypatch, tmp_path)

    result = _resolve_workspace(tmp_path)

    assert result == tmp_path


def test_generic_family_with_yaml_bypasses_detection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Generic mode with a kas_yaml skips the cwd walk entirely.

    _resolve_workspace(None, kas_yaml=..., family='generic') resolves to
    the YAML's parent directory without calling _workspace_from_cwd, so
    it works from a directory that would otherwise exit 2.
    """
    byo_dir = tmp_path / "my-byo-project"
    byo_dir.mkdir()
    kas_yaml = byo_dir / "kas.yml"
    kas_yaml.write_text("machine: qemuarm64\n")
    _chdir(monkeypatch, tmp_path)  # tmp_path has no bspctl signals

    result = _resolve_workspace(None, kas_yaml=kas_yaml, family="generic")

    assert result == byo_dir.resolve()
