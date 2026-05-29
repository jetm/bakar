"""Hermetic unit tests for ``bakar.workspace``.

Category B (filesystem + ``tmp_path``) tests for the parsing helpers and
state detection. ``subprocess.run`` is patched at the module-qualified
path (``bakar.workspace.subprocess.run``) so concurrent tests can never
race on a global stdlib patch.

Covers: ``parse_manifest_pins`` (happy path, empty manifest, non-SHA
revision filtering), ``read_repo_manifest_include`` (present and absent),
``find_sha_drift`` (drift and no-drift), ``_cache_dirs_ok``,
``ensure_tools`` (all-present and partially-missing), and ``_detect_nxp``
on a ``fake_workspace``.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from bakar import workspace
from bakar.config import resolve
from bakar.workspace import (
    _cache_dirs_ok,
    _detect_nxp,
    ensure_tools,
    find_sha_drift,
    parse_manifest_pins,
    read_repo_manifest_include,
)

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# parse_manifest_pins
# ---------------------------------------------------------------------------


def test_parse_manifest_pins_returns_both_pins(fake_workspace: Path) -> None:
    """The two synthetic ``<project>`` elements with 40-hex SHAs are returned."""
    manifest = fake_workspace / "nxp" / "imx-6.1.55-2.2.0.xml"

    pins = parse_manifest_pins(manifest)

    assert set(pins) == {
        ("sources/poky", "a" * 40),
        ("sources/meta-imx", "b" * 40),
    }


def test_parse_manifest_pins_empty_manifest(tmp_path: Path) -> None:
    """A manifest with no qualifying ``<project>`` elements returns ``[]``."""
    empty_manifest = tmp_path / "empty.xml"
    empty_manifest.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<manifest>\n"
        '  <remote name="origin" fetch="https://example.com"/>\n'
        '  <default revision="master" remote="origin"/>\n'
        "</manifest>\n"
    )

    assert parse_manifest_pins(empty_manifest) == []


def test_parse_manifest_pins_filters_non_hex40_revisions(tmp_path: Path) -> None:
    """A 7-char short SHA does not match ``_HEX40_RE`` and is excluded."""
    short_sha_manifest = tmp_path / "short.xml"
    short_sha_manifest.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<manifest>\n"
        '  <project path="sources/short" name="short" revision="abc1234"/>\n'
        f'  <project path="sources/full" name="full" revision="{"c" * 40}"/>\n'
        "</manifest>\n"
    )

    pins = parse_manifest_pins(short_sha_manifest)

    paths = {path for path, _ in pins}
    assert "sources/short" not in paths
    assert "sources/full" in paths


def test_parse_manifest_pins_missing_file_returns_empty(tmp_path: Path) -> None:
    """A non-existent manifest path returns ``[]`` (no crash)."""
    assert parse_manifest_pins(tmp_path / "does-not-exist.xml") == []


# ---------------------------------------------------------------------------
# read_repo_manifest_include
# ---------------------------------------------------------------------------


def test_read_repo_manifest_include_returns_included_filename(tmp_path: Path) -> None:
    """A ``.repo/`` dir with a ``manifest.xml`` containing ``<include name=...>``."""
    repo_dir = tmp_path / ".repo"
    repo_dir.mkdir()
    (repo_dir / "manifest.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<manifest>\n  <include name="imx-6.6.52-2.2.2.xml"/>\n</manifest>\n'
    )

    assert read_repo_manifest_include(repo_dir) == "imx-6.6.52-2.2.2.xml"


def test_read_repo_manifest_include_returns_none_when_absent(tmp_path: Path) -> None:
    """No ``manifest.xml`` file -> ``None``."""
    repo_dir = tmp_path / ".repo"
    repo_dir.mkdir()

    assert read_repo_manifest_include(repo_dir) is None


def test_read_repo_manifest_include_returns_none_without_include_element(
    tmp_path: Path,
) -> None:
    """A ``manifest.xml`` without an ``<include>`` element -> ``None``."""
    repo_dir = tmp_path / ".repo"
    repo_dir.mkdir()
    (repo_dir / "manifest.xml").write_text('<?xml version="1.0" encoding="UTF-8"?>\n<manifest></manifest>\n')

    assert read_repo_manifest_include(repo_dir) is None


# ---------------------------------------------------------------------------
# find_sha_drift
# ---------------------------------------------------------------------------


def _fake_completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["git"], returncode=returncode, stdout=stdout, stderr="")


def test_find_sha_drift_flags_mismatched_layer(tmp_path: Path) -> None:
    """One layer's HEAD SHA differs from the expected pin -> reported as drift."""
    workspace_root = tmp_path
    # _head_sha probes (checkout / ".git").exists(); create that marker.
    for layer in ("sources/poky", "sources/meta-imx"):
        (workspace_root / layer / ".git").mkdir(parents=True)

    pins = [
        ("sources/poky", "a" * 40),
        ("sources/meta-imx", "b" * 40),
    ]

    # First call: matches expected SHA. Second call: mismatched SHA.
    actual_for_meta_imx = "d" * 40
    fake_run_results = [
        _fake_completed("a" * 40 + "\n"),
        _fake_completed(actual_for_meta_imx + "\n"),
    ]

    with patch.object(workspace.subprocess, "run", side_effect=fake_run_results):
        drift = find_sha_drift(workspace_root, pins)

    assert drift == [("sources/meta-imx", "b" * 40, actual_for_meta_imx)]


def test_find_sha_drift_returns_empty_when_all_match(tmp_path: Path) -> None:
    """When every on-disk HEAD matches its pin, drift is empty."""
    workspace_root = tmp_path
    for layer in ("sources/poky", "sources/meta-imx"):
        (workspace_root / layer / ".git").mkdir(parents=True)

    pins = [
        ("sources/poky", "a" * 40),
        ("sources/meta-imx", "b" * 40),
    ]
    fake_run_results = [
        _fake_completed("a" * 40 + "\n"),
        _fake_completed("b" * 40 + "\n"),
    ]

    with patch.object(workspace.subprocess, "run", side_effect=fake_run_results):
        drift = find_sha_drift(workspace_root, pins)

    assert drift == []


def test_find_sha_drift_skips_uncloned_layers(tmp_path: Path) -> None:
    """A layer with no ``.git`` is treated as a future sync target, not drift."""
    pins = [("sources/never-cloned", "a" * 40)]
    # No directories created on disk.

    drift = find_sha_drift(tmp_path, pins)

    assert drift == []


# ---------------------------------------------------------------------------
# _cache_dirs_ok
# ---------------------------------------------------------------------------


def test_cache_dirs_ok_returns_bool() -> None:
    """The helper must return a ``bool`` regardless of the host environment."""
    result = _cache_dirs_ok()
    assert isinstance(result, bool)


def test_cache_dirs_ok_true_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """With SSTATE_DIR and DL_DIR unset, the helper has no paths to check -> True."""
    monkeypatch.delenv("SSTATE_DIR", raising=False)
    monkeypatch.delenv("DL_DIR", raising=False)
    assert _cache_dirs_ok() is True


def test_cache_dirs_ok_false_when_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A SSTATE_DIR pointing at a missing path returns False."""
    monkeypatch.setenv("SSTATE_DIR", str(tmp_path / "does-not-exist"))
    monkeypatch.delenv("DL_DIR", raising=False)
    assert _cache_dirs_ok() is False


# ---------------------------------------------------------------------------
# ensure_tools
# ---------------------------------------------------------------------------


def test_ensure_tools_returns_empty_when_all_present(fake_workspace: Path) -> None:
    """When ``shutil.which`` finds every binary, the missing list is empty."""
    cfg = resolve(workspace=fake_workspace, bsp_family="nxp")

    with patch.object(workspace.shutil, "which", return_value="/usr/bin/dummy"):
        assert ensure_tools(cfg) == []


def test_ensure_tools_reports_missing_binaries(fake_workspace: Path) -> None:
    """When ``shutil.which`` returns None for a tool, that tool is reported."""
    cfg = resolve(workspace=fake_workspace, bsp_family="nxp")

    # Map of "tool present?" -- repo and docker missing, the rest present.
    def fake_which(name: str) -> str | None:
        return None if name in {"repo", "docker"} else f"/usr/bin/{name}"

    with patch.object(workspace.shutil, "which", side_effect=fake_which):
        missing = ensure_tools(cfg)

    assert set(missing) == {"repo", "docker"}


def test_ensure_tools_ti_branch_requires_git(fake_workspace: Path) -> None:
    """TI branch picks ``git`` over ``repo`` in the required-tool list."""
    cfg = resolve(workspace=fake_workspace, bsp_family="ti")

    def fake_which(name: str) -> str | None:
        return None if name == "git" else f"/usr/bin/{name}"

    with patch.object(workspace.shutil, "which", side_effect=fake_which):
        missing = ensure_tools(cfg)

    assert missing == ["git"]


# ---------------------------------------------------------------------------
# _detect_nxp
# ---------------------------------------------------------------------------


def test_detect_nxp_populates_state(fake_workspace: Path) -> None:
    """A minimal NXP workspace yields a state with manifest and branch populated.

    The synthetic ``fake_workspace`` has neither ``.repo/`` nor a populated
    ``sources/poky``, so the manifest-include / branch-tracking fields are
    None and ``sha_drift`` stays empty - but the requested manifest and
    branch (taken from the resolved cfg) must round-trip into the state.
    Patches ``subprocess.run`` to a fixed SHA so the test cannot reach the
    real git binary.
    """
    cfg = resolve(
        workspace=fake_workspace,
        bsp_family="nxp",
        manifest="imx-6.1.55-2.2.0.xml",
    )

    with patch.object(workspace.subprocess, "run", return_value=_fake_completed("a" * 40 + "\n")):
        state = _detect_nxp(cfg)

    assert state.bsp_family == "nxp"
    assert state.requested_manifest == "imx-6.1.55-2.2.0.xml"
    # repo_branch is inferred from the manifest prefix; imx-6.1.* has no
    # mapping in BRANCH_BY_MANIFEST_PREFIX so it falls back to scarthgap.
    assert state.requested_branch == "scarthgap"
    # No .repo/ in the fake workspace -> include and branch tracking are None.
    assert state.repo_manifest_include is None
    assert state.repo_manifests_branch is None
    assert state.sources_populated is False
    assert state.sha_drift == ()
