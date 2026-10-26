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
    assert result.stderr.strip() == ""
    parsed = json.loads(result.stdout)
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

    assert result.stderr.strip() == ""
    parsed = json.loads(result.stdout)
    assert parsed, "Expected at least one entry"
    for obj in parsed:
        assert "pinned" in obj
        assert "actual" in obj
        assert "distance" in obj


def test_drift_json_payload_on_stdout(runner: CliRunner, tmp_path: Path, kas_yaml: Path) -> None:
    """The JSON document goes to stdout unwrapped, even with a path far wider than a terminal."""
    long_name = "meta-" + "x" * 200
    sources_dir = tmp_path / "sources"
    _make_git_repo(sources_dir / long_name)
    pinned_sha = "f" * 40

    lock = tmp_path / "kas.lock"
    lock.write_text(json.dumps({"repos": {long_name: {"commit": pinned_sha}}}))

    result = runner.invoke(app, ["drift", str(kas_yaml), "-w", str(tmp_path), "--json"])

    assert result.exit_code == 1, result.output
    assert result.stderr.strip() == ""
    parsed = json.loads(result.stdout)
    assert [e["source"] for e in parsed] == [long_name]
    assert long_name in result.stdout


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


# ---------------------------------------------------------------------------
# meta-avocado kas files (pins from the merged kas configuration)
# ---------------------------------------------------------------------------


def _meta_avocado_kas(tmp_path: Path) -> Path:
    kas_dir = tmp_path / "meta-avocado" / "kas" / "machine"
    kas_dir.mkdir(parents=True)
    path = kas_dir / "qemux86-64.yml"
    path.write_text("header:\n  version: 14\nmachine: qemux86-64\n")
    return path


def _patch_merged(repos: dict) -> object:
    return patch("bakar.commands.drift.kas_pins.read_merged_config", return_value={"repos": repos})


def test_drift_meta_avocado_reports_off_pin_clone(runner: CliRunner, tmp_path: Path) -> None:
    """A clone whose HEAD is not the merged-config pin is DRIFTED with a known distance."""
    ws = tmp_path / "ws"
    _make_git_repo(ws / "oe-core")
    first = _git_head(ws / "oe-core")
    (ws / "oe-core" / "README").write_text("y")
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    subprocess.run(
        ["git", "-C", str(ws / "oe-core"), "commit", "-qam", "second"], capture_output=True, check=True, env=env
    )
    head = _git_head(ws / "oe-core")
    kas = _meta_avocado_kas(tmp_path)

    with _patch_merged({"oe-core": {"url": "https://example.invalid/oe", "commit": first}}):
        result = runner.invoke(app, ["drift", str(kas), "-w", str(ws), "--json"])

    assert result.exit_code == 1, result.output
    parsed = json.loads(result.stdout)
    assert parsed == [
        {"source": "oe-core", "pinned": first, "actual": head, "distance": 1, "drifted": True, "missing": False}
    ]


def test_drift_meta_avocado_reports_missing_clone(runner: CliRunner, tmp_path: Path) -> None:
    """A pinned repository with no clone is missing and counts as drift."""
    ws = tmp_path / "ws"
    ws.mkdir()
    kas = _meta_avocado_kas(tmp_path)

    with _patch_merged({"oe-core": {"url": "https://example.invalid/oe", "commit": "a" * 40}}):
        result = runner.invoke(app, ["drift", str(kas), "-w", str(ws)])
        as_json = runner.invoke(app, ["drift", str(kas), "-w", str(ws), "--json"])

    assert result.exit_code == 1, result.output
    assert "missing" in result.output
    assert "DRIFTED" in result.output
    entry = json.loads(as_json.stdout)[0]
    assert entry["missing"] is True
    assert entry["actual"] is None
    assert entry["drifted"] is True


def test_drift_meta_avocado_unknown_distance_when_pin_absent(runner: CliRunner, tmp_path: Path) -> None:
    """A drifted clone lacking the pinned commit reports distance None, never zero."""
    ws = tmp_path / "ws"
    _make_git_repo(ws / "oe-core")
    kas = _meta_avocado_kas(tmp_path)

    with _patch_merged({"oe-core": {"url": "https://example.invalid/oe", "commit": "b" * 40}}):
        result = runner.invoke(app, ["drift", str(kas), "-w", str(ws), "--json"])

    assert result.exit_code == 1, result.output
    entry = json.loads(result.stdout)[0]
    assert entry["drifted"] is True
    assert entry["missing"] is False
    assert entry["distance"] is None


def test_drift_meta_avocado_counts_a_clone_behind_its_pin_negative(runner: CliRunner, tmp_path: Path) -> None:
    """A clone that is an ancestor of its pin is behind by N, not unknown."""
    ws = tmp_path / "ws"
    _make_git_repo(ws / "oe-core")
    behind_head = _git_head(ws / "oe-core")
    (ws / "oe-core" / "README").write_text("y")
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    subprocess.run(
        ["git", "-C", str(ws / "oe-core"), "commit", "-qam", "second"], capture_output=True, check=True, env=env
    )
    pin = _git_head(ws / "oe-core")
    subprocess.run(
        ["git", "-C", str(ws / "oe-core"), "checkout", "-q", "--detach", behind_head],
        capture_output=True,
        check=True,
        env=env,
    )
    kas = _meta_avocado_kas(tmp_path)

    with _patch_merged({"oe-core": {"url": "https://example.invalid/oe", "commit": pin}}):
        as_json = runner.invoke(app, ["drift", str(kas), "-w", str(ws), "--json"])
        as_text = runner.invoke(app, ["drift", str(kas), "-w", str(ws)])

    assert json.loads(as_json.stdout)[0]["distance"] == -1
    assert "-1" in as_text.output


def test_drift_meta_avocado_flags_a_dirty_bitbake_at_its_pin(runner: CliRunner, tmp_path: Path) -> None:
    """The build refuses a dirty bitbake even at its pin, so drift reports it instead of 'clean'."""
    ws = tmp_path / "ws"
    _make_git_repo(ws / "bitbake")
    pin = _git_head(ws / "bitbake")
    (ws / "bitbake" / "README").write_text("local edit")
    kas = _meta_avocado_kas(tmp_path)

    with _patch_merged({"bitbake": {"url": "https://example.invalid/bb", "commit": pin}}):
        result = runner.invoke(app, ["drift", str(kas), "-w", str(ws), "--json"])

    assert result.exit_code == 1, result.output
    entry = json.loads(result.stdout)[0]
    assert entry["drifted"] is True
    assert entry["pinned"] == pin
    assert entry["actual"] == pin
    assert entry["distance"] == 0


def test_drift_meta_avocado_flags_an_entry_without_a_pin(runner: CliRunner, tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    _make_git_repo(ws / "meta-x")
    kas = _meta_avocado_kas(tmp_path)

    with _patch_merged({"meta-x": {"url": "https://example.invalid/x"}}):
        result = runner.invoke(app, ["drift", str(kas), "-w", str(ws), "--json"])

    assert result.exit_code == 1, result.output
    entry = json.loads(result.stdout)[0]
    assert entry["drifted"] is True
    assert entry["pinned"] is None


def _frozen_bitbake_and_dirty_core(ws: Path) -> dict:
    """A clean bitbake at its pin and an openembedded-core at its pin with a local edit."""
    _make_git_repo(ws / "bitbake")
    _make_git_repo(ws / "openembedded-core")
    (ws / "openembedded-core" / "README").write_text("local edit")
    return {
        "bitbake": {"url": "https://example.invalid/bb", "commit": _git_head(ws / "bitbake")},
        "openembedded-core": {"url": "https://example.invalid/oe", "commit": _git_head(ws / "openembedded-core")},
    }


@pytest.mark.parametrize(("container", "core_drifted"), [("1", True), ("0", False)])
def test_drift_meta_avocado_pairs_core_with_bitbake_only_in_host_mode(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, container: str, core_drifted: bool
) -> None:
    """A container build refuses a dirty openembedded-core a host build pairs, so drift follows the mode."""
    ws = tmp_path / "ws"
    repos = _frozen_bitbake_and_dirty_core(ws)
    kas = _meta_avocado_kas(tmp_path)
    monkeypatch.setenv("BAKAR_CONTAINER", container)

    with _patch_merged(repos):
        result = runner.invoke(app, ["drift", str(kas), "-w", str(ws), "--json", "--all"])

    by_name = {e["source"]: e for e in json.loads(result.stdout)}
    assert by_name["openembedded-core"]["drifted"] is core_drifted
    assert by_name["bitbake"]["drifted"] is False
    assert result.exit_code == (1 if core_drifted else 0), result.output


def test_drift_meta_avocado_global_container_flag_reports_dirty_core(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = tmp_path / "ws"
    repos = _frozen_bitbake_and_dirty_core(ws)
    kas = _meta_avocado_kas(tmp_path)
    monkeypatch.delenv("BAKAR_CONTAINER", raising=False)

    with _patch_merged(repos):
        result = runner.invoke(app, ["--container", "drift", str(kas), "-w", str(ws), "--json"])

    assert result.exit_code == 1, result.output
    assert [e["source"] for e in json.loads(result.stdout)] == ["openembedded-core"]


@pytest.mark.parametrize("fmt", ["text", "md"])
def test_drift_meta_avocado_prints_names_without_markup_or_control_characters(
    runner: CliRunner, tmp_path: Path, fmt: str
) -> None:
    """A kas key is printed literally: Rich markup is not parsed and control characters become '?'."""
    ws = tmp_path / "ws"
    ws.mkdir()
    kas = _meta_avocado_kas(tmp_path)
    repos = {
        "meta-x[/]": {"url": "https://example.invalid/x"},
        "meta-\x1b[2Ky": {"url": "https://example.invalid/y", "commit": "\x1b[1A" + "a" * 36},
    }

    with _patch_merged(repos):
        result = runner.invoke(app, ["drift", str(kas), "-w", str(ws), "--format", fmt])

    assert result.exit_code == 1, (result.output, result.exception)
    assert "meta-x[/]" in result.output
    assert "meta-?[2Ky" in result.output
    assert "?[1Aaaaa" in result.output
    assert "\x1b" not in result.output
