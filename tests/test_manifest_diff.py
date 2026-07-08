"""Unit tests for bakar.manifest_diff.diff_manifests.

Pinned manifests are real XML files written under ``tmp_path``;
``parse_manifest_pins`` reads each ``<project>`` element's ``path`` and
``revision`` (a 40-hex SHA). The ``git rev-list`` commit-count path is
mocked because CI has no synced layer checkouts (pattern from
``tests/test_layer_hashes.py``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from bakar.manifest_diff import diff_manifests
from tests._fakes import Completed as _Completed

if TYPE_CHECKING:
    from pathlib import Path


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# 40-hex SHAs so parse_manifest_pins' _HEX40_RE accepts them.
_SHA_A = "a" * 40
_SHA_B = "b" * 40
_SHA_C = "c" * 40


def _write_manifest(path: Path, projects: list[tuple[str, str]]) -> None:
    """Write a repo-tool manifest pinning each (path, revision) project."""
    lines = ["<manifest>"]
    for proj_path, rev in projects:
        lines.append(f'  <project path="{proj_path}" revision="{rev}"/>')
    lines.append("</manifest>")
    path.write_text("\n".join(lines) + "\n")


def _by_layer(diffs):
    """Index a LayerDiff list by its layer name."""
    return {d.layer: d for d in diffs}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_changed_layer_yields_distinct_shas(tmp_path: Path) -> None:
    """A layer with different SHAs reports both old and new (no checkout)."""
    old = tmp_path / "old.xml"
    new = tmp_path / "new.xml"
    _write_manifest(old, [("sources/poky", _SHA_A)])
    _write_manifest(new, [("sources/poky", _SHA_B)])

    diffs = _by_layer(diff_manifests(old, new))
    poky = diffs["sources/poky"]

    assert poky.old_sha == _SHA_A
    assert poky.new_sha == _SHA_B
    assert poky.old_sha != poky.new_sha


def test_unchanged_layer_has_equal_shas_and_zero_count(tmp_path: Path) -> None:
    """A layer with the same SHA on both sides gets commit_count == 0."""
    old = tmp_path / "old.xml"
    new = tmp_path / "new.xml"
    _write_manifest(old, [("sources/meta-freescale", _SHA_C)])
    _write_manifest(new, [("sources/meta-freescale", _SHA_C)])

    diffs = _by_layer(diff_manifests(old, new))
    layer = diffs["sources/meta-freescale"]

    assert layer.old_sha == layer.new_sha == _SHA_C
    assert layer.commit_count == 0


def test_layer_only_in_new_manifest_has_none_old_sha(tmp_path: Path) -> None:
    """A layer present only in the new manifest reports old_sha is None."""
    old = tmp_path / "old.xml"
    new = tmp_path / "new.xml"
    _write_manifest(old, [("sources/poky", _SHA_A)])
    _write_manifest(new, [("sources/poky", _SHA_A), ("sources/meta-new", _SHA_B)])

    diffs = _by_layer(diff_manifests(old, new))
    added = diffs["sources/meta-new"]

    assert added.old_sha is None
    assert added.new_sha == _SHA_B
    assert added.commit_count is None


def test_changed_layer_without_checkout_root_has_none_count(tmp_path: Path) -> None:
    """Without a checkout_root, a changed layer's count is None, no exception."""
    old = tmp_path / "old.xml"
    new = tmp_path / "new.xml"
    _write_manifest(old, [("sources/poky", _SHA_A)])
    _write_manifest(new, [("sources/poky", _SHA_B)])

    diffs = _by_layer(diff_manifests(old, new))
    poky = diffs["sources/poky"]

    assert poky.commit_count is None


def test_changed_layer_with_checkout_uses_rev_list(tmp_path: Path) -> None:
    """With a present checkout, the count comes from git rev-list --count."""
    old = tmp_path / "old.xml"
    new = tmp_path / "new.xml"
    _write_manifest(old, [("sources/poky", _SHA_A)])
    _write_manifest(new, [("sources/poky", _SHA_B)])

    checkout_root = tmp_path / "sources"
    (checkout_root / "sources" / "poky").mkdir(parents=True)

    def fake_run(argv, **kwargs):
        assert "rev-list" in argv
        assert "--count" in argv
        assert f"{_SHA_A}..{_SHA_B}" in argv
        return _Completed(0, "5\n")

    with patch("bakar.gitutil.subprocess.run", side_effect=fake_run) as run:
        diffs = _by_layer(diff_manifests(old, new, checkout_root=checkout_root))

    assert diffs["sources/poky"].commit_count == 5
    run.assert_called_once()
