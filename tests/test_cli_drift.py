"""End-to-end CliRunner tests for ``bakar drift``.

These tests use temporary git repos and manifest XMLs to exercise the drift
detection logic without touching real workspace state. All git and subprocess
calls go through actual git binaries when creating the fake checkouts.
"""

from __future__ import annotations

import json
import os
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


def _make_git_repo(path: Path) -> None:
    """Create a minimal git repo whose HEAD resolves to a real SHA.

    Uses ``git init`` and ``git commit`` so ``git rev-parse HEAD`` returns a
    real 40-hex SHA.
    """
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", str(path)], capture_output=True, check=True, env=env)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@test.com"],
        capture_output=True,
        check=True,
        env=env,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Test"],
        capture_output=True,
        check=True,
        env=env,
    )
    (path / "README").write_text("x")
    subprocess.run(["git", "-C", str(path), "add", "README"], capture_output=True, check=True, env=env)
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", "init"],
        capture_output=True,
        check=True,
        env=env,
    )


def _git_head(path: Path) -> str:
    """Return the HEAD SHA of a repo."""
    out = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


@pytest.fixture
def kas_yaml(tmp_path: Path) -> Path:
    """Write a minimal kas YAML and return its path."""
    path = tmp_path / "machine.yml"
    path.write_text("header:\n  version: 14\nmachine: qemux86-64\n")
    return path


# ---------------------------------------------------------------------------
# bbsetup family (BYO via kas YAML + lockfile)
# ---------------------------------------------------------------------------


def test_drift_no_sources_reports_clean(runner: CliRunner, tmp_path: Path, kas_yaml: Path) -> None:
    """When the workspace has no source repos, the command exits 0 with a clean message."""
    with patch("bakar.commands.drift.discover_source_repos", return_value=[]):
        result = runner.invoke(app, ["drift", str(kas_yaml), "-w", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "All sources are on their pinned revision" in result.output


def test_drift_clean_source_excluded_by_default(runner: CliRunner, tmp_path: Path, kas_yaml: Path) -> None:
    """A source at its pinned SHA is not listed without --all."""
    sources_dir = tmp_path / "sources"
    _make_git_repo(sources_dir / "poky")
    actual_sha = _git_head(sources_dir / "poky")

    # Write a lockfile that pins to the same SHA as actual HEAD
    lock = tmp_path / "kas.lock"
    lock.write_text(json.dumps({"repos": {"poky": {"commit": actual_sha}}}))

    result = runner.invoke(app, ["drift", str(kas_yaml), "-w", str(tmp_path)])

    assert result.exit_code == 0, result.output
    # "poky" should not appear because it's not drifted
    assert "DRIFTED" not in result.output


def test_drift_drifted_source_listed(runner: CliRunner, tmp_path: Path, kas_yaml: Path) -> None:
    """A source whose HEAD differs from its lockfile pin is reported as DRIFTED."""
    sources_dir = tmp_path / "sources"
    _make_git_repo(sources_dir / "poky")
    actual_sha = _git_head(sources_dir / "poky")

    # Pin to a DIFFERENT sha to force drift
    pinned_sha = "c" * 40
    lock = tmp_path / "kas.lock"
    lock.write_text(json.dumps({"repos": {"poky": {"commit": pinned_sha}}}))

    result = runner.invoke(app, ["drift", str(kas_yaml), "-w", str(tmp_path)])

    assert result.exit_code == 1, result.output
    assert "poky" in result.output
    assert "DRIFTED" in result.output
    assert actual_sha[:8] in result.output
    assert pinned_sha[:8] in result.output


def test_drift_all_flag_shows_clean_sources(runner: CliRunner, tmp_path: Path, kas_yaml: Path) -> None:
    """--all includes clean (non-drifted) sources in the output."""
    sources_dir = tmp_path / "sources"
    _make_git_repo(sources_dir / "poky")
    actual_sha = _git_head(sources_dir / "poky")

    lock = tmp_path / "kas.lock"
    lock.write_text(json.dumps({"repos": {"poky": {"commit": actual_sha}}}))

    result = runner.invoke(app, ["drift", str(kas_yaml), "-w", str(tmp_path), "--all"])

    assert result.exit_code == 0, result.output
    assert "poky" in result.output
    assert "clean" in result.output


def test_drift_json_output_parseable(runner: CliRunner, tmp_path: Path, kas_yaml: Path) -> None:
    """--json emits a JSON array parseable by json.loads."""
    sources_dir = tmp_path / "sources"
    _make_git_repo(sources_dir / "poky")
    actual_sha = _git_head(sources_dir / "poky")
    pinned_sha = "d" * 40

    lock = tmp_path / "kas.lock"
    lock.write_text(json.dumps({"repos": {"poky": {"commit": pinned_sha}}}))

    result = runner.invoke(app, ["drift", str(kas_yaml), "-w", str(tmp_path), "--json"])

    assert result.exit_code == 1, result.output
    parsed = json.loads(result.output.strip())
    assert isinstance(parsed, list)
    assert len(parsed) == 1
    entry = parsed[0]
    assert entry["source"] == "poky"
    assert entry["pinned"] == pinned_sha
    assert entry["actual"] == actual_sha
    assert "distance" in entry


def test_drift_json_keys_present(runner: CliRunner, tmp_path: Path, kas_yaml: Path) -> None:
    """JSON output objects contain pinned, actual, and distance keys."""
    sources_dir = tmp_path / "sources"
    _make_git_repo(sources_dir / "meta-foo")
    pinned_sha = "e" * 40

    lock = tmp_path / "kas.lock"
    lock.write_text(json.dumps({"repos": {"meta-foo": {"commit": pinned_sha}}}))

    result = runner.invoke(
        app,
        ["drift", str(kas_yaml), "-w", str(tmp_path), "--json"],
    )

    parsed = json.loads(result.output.strip())
    assert parsed, "Expected at least one entry"
    for obj in parsed:
        assert "pinned" in obj
        assert "actual" in obj
        assert "distance" in obj


def test_drift_markdown_format(runner: CliRunner, tmp_path: Path, kas_yaml: Path) -> None:
    """--format md emits a markdown table."""
    sources_dir = tmp_path / "sources"
    _make_git_repo(sources_dir / "poky")
    pinned_sha = "f" * 40

    lock = tmp_path / "kas.lock"
    lock.write_text(json.dumps({"repos": {"poky": {"commit": pinned_sha}}}))

    result = runner.invoke(
        app,
        ["drift", str(kas_yaml), "-w", str(tmp_path), "--format", "md"],
    )

    assert result.exit_code == 1, result.output
    assert "|" in result.output
    assert "Source" in result.output
    assert "Pinned" in result.output


# ---------------------------------------------------------------------------
# NXP family (manifest-based pins)
# ---------------------------------------------------------------------------


def test_drift_nxp_missing_manifest_exits_2(runner: CliRunner, tmp_path: Path) -> None:
    """NXP family: when the manifest XML is absent, exit code is 2 with a message."""
    ws = tmp_path
    nxp_dir = ws / "nxp"
    nxp_dir.mkdir()

    result = runner.invoke(
        app,
        ["drift", "-f", "imx-6.6.52-2.2.2.xml", "-w", str(ws)],
    )

    assert result.exit_code == 2, result.output
    # Should mention the missing input
    assert "Manifest not found" in result.output


def test_drift_nxp_reads_manifest_pins(runner: CliRunner, tmp_path: Path) -> None:
    """NXP family: pins are read from the manifest XML, not a lockfile."""
    ws = tmp_path
    nxp_dir = ws / "nxp"
    manifests_dir = nxp_dir / ".repo" / "manifests"
    manifests_dir.mkdir(parents=True)

    # Write a manifest with a valid NXP filename pattern
    manifest_name = "imx-6.6.52-2.2.2.xml"
    manifest = manifests_dir / manifest_name
    pinned_sha = "a" * 40
    _write_manifest(manifest, [("sources/poky", pinned_sha)])

    # Create a real git repo for the source
    sources_dir = nxp_dir / "sources"
    _make_git_repo(sources_dir / "poky")
    actual_sha = _git_head(sources_dir / "poky")

    # The source HEAD differs from the pinned sha -> drift reported
    result = runner.invoke(
        app,
        ["drift", "-f", manifest_name, "-w", str(ws)],
    )

    # Whether drifted or clean, the command should succeed (not exit 2)
    assert result.exit_code in (0, 1), result.output
    if actual_sha != pinned_sha:
        assert "poky" in result.output


# ---------------------------------------------------------------------------
# Exit code semantics
# ---------------------------------------------------------------------------


def test_drift_exits_0_when_no_drift(runner: CliRunner, tmp_path: Path, kas_yaml: Path) -> None:
    """Exit code is 0 when all sources match their pinned SHA."""
    with patch("bakar.commands.drift.discover_source_repos", return_value=[]):
        result = runner.invoke(app, ["drift", str(kas_yaml), "-w", str(tmp_path)])

    assert result.exit_code == 0


def test_drift_exits_nonzero_when_drift_detected(runner: CliRunner, tmp_path: Path, kas_yaml: Path) -> None:
    """Exit code is non-zero when at least one source has drifted."""
    sources_dir = tmp_path / "sources"
    _make_git_repo(sources_dir / "poky")

    pinned_sha = "0" * 40
    lock = tmp_path / "kas.lock"
    lock.write_text(json.dumps({"repos": {"poky": {"commit": pinned_sha}}}))

    result = runner.invoke(app, ["drift", str(kas_yaml), "-w", str(tmp_path)])

    assert result.exit_code != 0, result.output
