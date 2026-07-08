"""End-to-end CliRunner tests for ``bakar changelog``.

Uses temporary files and git repos to exercise the pin-comparison logic
without touching real workspace state.
"""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from bakar.cli import app

if TYPE_CHECKING:
    from pathlib import Path

    from typer.testing import CliRunner

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_manifest(path: Path, projects: list[tuple[str, str]]) -> None:
    """Write a minimal repo manifest XML."""
    lines = ["<manifest>"]
    for proj_path, rev in projects:
        lines.append(f'  <project path="{proj_path}" revision="{rev}"/>')
    lines.append("</manifest>")
    path.write_text("\n".join(lines) + "\n")


def _write_lockfile(path: Path, repos: dict[str, str]) -> None:
    """Write a minimal kas lockfile JSON."""
    path.write_text(json.dumps({"repos": {name: {"commit": sha} for name, sha in repos.items()}}))


def _make_git_repo(path: Path) -> str:
    """Create a minimal git repo and return its HEAD SHA."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", str(path)], capture_output=True, check=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@test.com"],
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Test"],
        capture_output=True,
        check=True,
    )
    (path / "README").write_text("x")
    subprocess.run(["git", "-C", str(path), "add", "README"], capture_output=True, check=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", "init"],
        capture_output=True,
        check=True,
    )
    out = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


_SHA_A = "a" * 40
_SHA_B = "b" * 40
_SHA_C = "c" * 40


# ---------------------------------------------------------------------------
# Manifest-to-manifest diffing (NXP/TI style)
# ---------------------------------------------------------------------------


def test_changelog_added_layer(runner: CliRunner, tmp_path: Path) -> None:
    """A layer present only in <to> appears in the Added section."""
    from_xml = tmp_path / "from.xml"
    to_xml = tmp_path / "to.xml"

    _write_manifest(from_xml, [("sources/poky", _SHA_A)])
    _write_manifest(to_xml, [("sources/poky", _SHA_A), ("sources/meta-imx", _SHA_B)])

    with (
        patch("bakar.commands.changelog._dispatch_bsp", return_value=("bbsetup", None)),
        patch("bakar.commands.changelog._resolve_workspace", return_value=tmp_path),
        patch("bakar.commands.changelog.resolve") as mock_resolve,
    ):
        mock_cfg = mock_resolve.return_value
        mock_cfg.bsp_root = tmp_path
        result = runner.invoke(app, ["changelog", str(from_xml), str(to_xml), "-w", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "Added" in result.output
    assert "meta-imx" in result.output


def test_changelog_removed_layer(runner: CliRunner, tmp_path: Path) -> None:
    """A layer present only in <from> appears in the Removed section."""
    from_xml = tmp_path / "from.xml"
    to_xml = tmp_path / "to.xml"

    _write_manifest(from_xml, [("sources/poky", _SHA_A), ("sources/meta-imx", _SHA_B)])
    _write_manifest(to_xml, [("sources/poky", _SHA_A)])

    with (
        patch("bakar.commands.changelog._dispatch_bsp", return_value=("bbsetup", None)),
        patch("bakar.commands.changelog._resolve_workspace", return_value=tmp_path),
        patch("bakar.commands.changelog.resolve") as mock_resolve,
    ):
        mock_cfg = mock_resolve.return_value
        mock_cfg.bsp_root = tmp_path
        result = runner.invoke(app, ["changelog", str(from_xml), str(to_xml), "-w", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "Removed" in result.output
    assert "meta-imx" in result.output


def test_changelog_modified_layer(runner: CliRunner, tmp_path: Path) -> None:
    """A layer whose SHA changed appears in the Modified section."""
    from_xml = tmp_path / "from.xml"
    to_xml = tmp_path / "to.xml"

    _write_manifest(from_xml, [("sources/poky", _SHA_A)])
    _write_manifest(to_xml, [("sources/poky", _SHA_B)])

    with (
        patch("bakar.commands.changelog._dispatch_bsp", return_value=("bbsetup", None)),
        patch("bakar.commands.changelog._resolve_workspace", return_value=tmp_path),
        patch("bakar.commands.changelog.resolve") as mock_resolve,
    ):
        mock_cfg = mock_resolve.return_value
        mock_cfg.bsp_root = tmp_path
        result = runner.invoke(app, ["changelog", str(from_xml), str(to_xml), "-w", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "Modified" in result.output
    assert "poky" in result.output
    assert _SHA_A[:8] in result.output
    assert _SHA_B[:8] in result.output


def test_changelog_unchanged_layer_omitted(runner: CliRunner, tmp_path: Path) -> None:
    """An unchanged layer (same SHA in both inputs) does NOT appear in output."""
    from_xml = tmp_path / "from.xml"
    to_xml = tmp_path / "to.xml"

    _write_manifest(from_xml, [("sources/poky", _SHA_A), ("sources/meta-imx", _SHA_B)])
    _write_manifest(to_xml, [("sources/poky", _SHA_A), ("sources/meta-imx", _SHA_C)])

    with (
        patch("bakar.commands.changelog._dispatch_bsp", return_value=("bbsetup", None)),
        patch("bakar.commands.changelog._resolve_workspace", return_value=tmp_path),
        patch("bakar.commands.changelog.resolve") as mock_resolve,
    ):
        mock_cfg = mock_resolve.return_value
        mock_cfg.bsp_root = tmp_path
        result = runner.invoke(app, ["changelog", str(from_xml), str(to_xml), "-w", str(tmp_path)])

    assert result.exit_code == 0, result.output
    # poky is unchanged (both _SHA_A) - should NOT appear in output
    assert "poky" not in result.output
    # meta-imx changed - should appear
    assert "meta-imx" in result.output


def test_changelog_no_changes(runner: CliRunner, tmp_path: Path) -> None:
    """When both states are identical, report no changes."""
    from_xml = tmp_path / "from.xml"
    to_xml = tmp_path / "to.xml"

    _write_manifest(from_xml, [("sources/poky", _SHA_A)])
    _write_manifest(to_xml, [("sources/poky", _SHA_A)])

    with (
        patch("bakar.commands.changelog._dispatch_bsp", return_value=("bbsetup", None)),
        patch("bakar.commands.changelog._resolve_workspace", return_value=tmp_path),
        patch("bakar.commands.changelog.resolve") as mock_resolve,
    ):
        mock_cfg = mock_resolve.return_value
        mock_cfg.bsp_root = tmp_path
        result = runner.invoke(app, ["changelog", str(from_xml), str(to_xml), "-w", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "No changes" in result.output


# ---------------------------------------------------------------------------
# Kas lockfile diffing (BYO/bbsetup style)
# ---------------------------------------------------------------------------


def test_changelog_lockfile_added(runner: CliRunner, tmp_path: Path) -> None:
    """A repo added in the to-lockfile appears in Added."""
    from_lock = tmp_path / "from.lock"
    to_lock = tmp_path / "to.lock"

    _write_lockfile(from_lock, {"poky": _SHA_A})
    _write_lockfile(to_lock, {"poky": _SHA_A, "meta-avocado": _SHA_B})

    with (
        patch("bakar.commands.changelog._dispatch_bsp", return_value=("bbsetup", None)),
        patch("bakar.commands.changelog._resolve_workspace", return_value=tmp_path),
        patch("bakar.commands.changelog.resolve") as mock_resolve,
    ):
        mock_cfg = mock_resolve.return_value
        mock_cfg.bsp_root = tmp_path
        result = runner.invoke(app, ["changelog", str(from_lock), str(to_lock), "-w", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "Added" in result.output
    assert "meta-avocado" in result.output


def test_changelog_lockfile_removed(runner: CliRunner, tmp_path: Path) -> None:
    """A repo absent in the to-lockfile appears in Removed."""
    from_lock = tmp_path / "from.lock"
    to_lock = tmp_path / "to.lock"

    _write_lockfile(from_lock, {"poky": _SHA_A, "meta-avocado": _SHA_B})
    _write_lockfile(to_lock, {"poky": _SHA_A})

    with (
        patch("bakar.commands.changelog._dispatch_bsp", return_value=("bbsetup", None)),
        patch("bakar.commands.changelog._resolve_workspace", return_value=tmp_path),
        patch("bakar.commands.changelog.resolve") as mock_resolve,
    ):
        mock_cfg = mock_resolve.return_value
        mock_cfg.bsp_root = tmp_path
        result = runner.invoke(app, ["changelog", str(from_lock), str(to_lock), "-w", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "Removed" in result.output
    assert "meta-avocado" in result.output


def test_changelog_lockfile_modified(runner: CliRunner, tmp_path: Path) -> None:
    """A repo with a changed SHA in the lockfile appears in Modified."""
    from_lock = tmp_path / "from.lock"
    to_lock = tmp_path / "to.lock"

    _write_lockfile(from_lock, {"poky": _SHA_A})
    _write_lockfile(to_lock, {"poky": _SHA_B})

    with (
        patch("bakar.commands.changelog._dispatch_bsp", return_value=("bbsetup", None)),
        patch("bakar.commands.changelog._resolve_workspace", return_value=tmp_path),
        patch("bakar.commands.changelog.resolve") as mock_resolve,
    ):
        mock_cfg = mock_resolve.return_value
        mock_cfg.bsp_root = tmp_path
        result = runner.invoke(app, ["changelog", str(from_lock), str(to_lock), "-w", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "Modified" in result.output
    assert "poky" in result.output


# ---------------------------------------------------------------------------
# Falsifier: XML input is NOT parsed as a kas lockfile
# ---------------------------------------------------------------------------


def test_changelog_xml_not_parsed_as_lockfile(runner: CliRunner, tmp_path: Path) -> None:
    """A .xml input is parsed as manifest XML, not a kas lockfile."""
    from_xml = tmp_path / "from.xml"
    to_xml = tmp_path / "to.xml"

    _write_manifest(from_xml, [("sources/poky", _SHA_A)])
    _write_manifest(to_xml, [("sources/poky", _SHA_B)])

    from bakar.commands.changelog import _is_kas_lockfile, _is_manifest_xml

    assert _is_manifest_xml(from_xml) is True
    # An XML file must NOT be detected as a kas lockfile
    assert _is_kas_lockfile(from_xml) is False


# ---------------------------------------------------------------------------
# Markdown format
# ---------------------------------------------------------------------------


def test_changelog_markdown_heading(runner: CliRunner, tmp_path: Path) -> None:
    """--format md starts with a heading naming the from/to states."""
    from_xml = tmp_path / "from.xml"
    to_xml = tmp_path / "to.xml"

    _write_manifest(from_xml, [("sources/poky", _SHA_A)])
    _write_manifest(to_xml, [("sources/poky", _SHA_B)])

    with (
        patch("bakar.commands.changelog._dispatch_bsp", return_value=("bbsetup", None)),
        patch("bakar.commands.changelog._resolve_workspace", return_value=tmp_path),
        patch("bakar.commands.changelog.resolve") as mock_resolve,
    ):
        mock_cfg = mock_resolve.return_value
        mock_cfg.bsp_root = tmp_path
        result = runner.invoke(
            app,
            ["changelog", str(from_xml), str(to_xml), "-w", str(tmp_path), "--format", "md"],
        )

    assert result.exit_code == 0, result.output
    # Output must contain a markdown heading naming the from/to states.
    # Rich may wrap long paths mid-character; collapse newlines before checking.
    flat = result.output.replace("\n", "")
    assert "## Changelog:" in flat
    # The from_xml basename must appear (Rich may split 'from.xml' across lines)
    assert "from" in flat


def test_changelog_markdown_no_changes(runner: CliRunner, tmp_path: Path) -> None:
    """--format md with identical states emits the no-changes italic note."""
    from_xml = tmp_path / "from.xml"
    to_xml = tmp_path / "to.xml"

    _write_manifest(from_xml, [("sources/poky", _SHA_A)])
    _write_manifest(to_xml, [("sources/poky", _SHA_A)])

    with (
        patch("bakar.commands.changelog._dispatch_bsp", return_value=("bbsetup", None)),
        patch("bakar.commands.changelog._resolve_workspace", return_value=tmp_path),
        patch("bakar.commands.changelog.resolve") as mock_resolve,
    ):
        mock_cfg = mock_resolve.return_value
        mock_cfg.bsp_root = tmp_path
        result = runner.invoke(
            app,
            ["changelog", str(from_xml), str(to_xml), "-w", str(tmp_path), "--format", "md"],
        )

    assert result.exit_code == 0, result.output
    assert "No changes" in result.output


# ---------------------------------------------------------------------------
# Commit log excerpt for Modified layers
# ---------------------------------------------------------------------------


def test_changelog_modified_with_real_commits(runner: CliRunner, tmp_path: Path) -> None:
    """Modified layers with a real git checkout show a git log excerpt."""
    # Create a real git repo with two commits
    sources_dir = tmp_path / "sources"
    repo = sources_dir / "poky"
    sha_old = _make_git_repo(repo)

    # Make a second commit
    (repo / "file2").write_text("y")
    subprocess.run(["git", "-C", str(repo), "add", "file2"], capture_output=True, check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "second commit"],
        capture_output=True,
        check=True,
    )
    out = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    sha_new = out.stdout.strip()

    from_lock = tmp_path / "from.lock"
    to_lock = tmp_path / "to.lock"
    _write_lockfile(from_lock, {"poky": sha_old})
    _write_lockfile(to_lock, {"poky": sha_new})

    with (
        patch("bakar.commands.changelog._dispatch_bsp", return_value=("bbsetup", None)),
        patch("bakar.commands.changelog._resolve_workspace", return_value=tmp_path),
        patch("bakar.commands.changelog.resolve") as mock_resolve,
    ):
        mock_cfg = mock_resolve.return_value
        mock_cfg.bsp_root = tmp_path
        result = runner.invoke(app, ["changelog", str(from_lock), str(to_lock), "-w", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "Modified" in result.output
    assert "poky" in result.output
    # The git log excerpt should show the second commit
    assert "second commit" in result.output


# ---------------------------------------------------------------------------
# _normalize_pins: manifest strips prefix, lockfile keeps bare names
# ---------------------------------------------------------------------------


def test_normalize_pins_manifest_strips_prefix() -> None:
    """Manifest pins with 'sources/<name>' prefix are stripped to bare names."""
    from bakar.commands.changelog import _normalize_pins

    raw = {"sources/meta-imx": "aaaa" * 10, "sources/poky": "bbbb" * 10}
    result = _normalize_pins(raw, is_manifest=True)
    assert "meta-imx" in result
    assert "poky" in result
    assert "sources/meta-imx" not in result


def test_normalize_pins_lockfile_keeps_bare_names() -> None:
    """Lockfile pins are already bare names and pass through unchanged."""
    from bakar.commands.changelog import _normalize_pins

    raw = {"meta-avocado": "aaaa" * 10, "poky": "bbbb" * 10}
    result = _normalize_pins(raw, is_manifest=False)
    assert result == raw


# ---------------------------------------------------------------------------
# Exit code: always 0 on success
# ---------------------------------------------------------------------------


def test_changelog_exits_0_on_success(runner: CliRunner, tmp_path: Path) -> None:
    """bakar changelog exits 0 regardless of how many changes are found."""
    from_xml = tmp_path / "from.xml"
    to_xml = tmp_path / "to.xml"

    _write_manifest(from_xml, [("sources/poky", _SHA_A)])
    _write_manifest(to_xml, [("sources/poky", _SHA_B), ("sources/meta-imx", _SHA_C)])

    with (
        patch("bakar.commands.changelog._dispatch_bsp", return_value=("bbsetup", None)),
        patch("bakar.commands.changelog._resolve_workspace", return_value=tmp_path),
        patch("bakar.commands.changelog.resolve") as mock_resolve,
    ):
        mock_cfg = mock_resolve.return_value
        mock_cfg.bsp_root = tmp_path
        result = runner.invoke(app, ["changelog", str(from_xml), str(to_xml), "-w", str(tmp_path)])

    assert result.exit_code == 0, result.output
