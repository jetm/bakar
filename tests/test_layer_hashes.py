"""Unit tests for bspctl.layers.collect_layer_hashes.

All git calls are mocked - CI has no synced BSP layer checkouts. The mock
keys on the git subcommand (``rev-parse`` vs ``branch``) and the ``-C`` path
so the assertions do not depend on dict iteration order.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from bspctl.config import resolve
from bspctl.layers import LayerHash, collect_layer_hashes

if TYPE_CHECKING:
    from pathlib import Path


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _nxp_cfg(tmp_path: Path):
    """Resolve an nxp BuildConfig rooted at a tmp_path workspace."""
    (tmp_path / "nxp").mkdir(parents=True, exist_ok=True)
    return resolve(workspace=tmp_path, bsp_family="nxp")


def _write_bblayers(cfg, repos: list[str]) -> None:
    """Write a bblayers.conf naming each repo under /work/sources/<repo>."""
    conf = cfg.bblayers_conf
    conf.parent.mkdir(parents=True, exist_ok=True)
    lines = ['BBLAYERS ?= " \\']
    for repo in repos:
        lines.append(f"  /work/sources/{repo}/meta-{repo} \\")
    lines.append('"')
    conf.write_text("\n".join(lines) + "\n")


def _make_source_dirs(cfg, repos: list[str]) -> None:
    """Create the sources/<repo> directories that is_dir() checks against."""
    for repo in repos:
        (cfg.bsp_root / "sources" / repo).mkdir(parents=True, exist_ok=True)


class _Completed:
    """Minimal stand-in for subprocess.CompletedProcess."""

    def __init__(self, returncode: int, stdout: str) -> None:
        self.returncode = returncode
        self.stdout = stdout


def _git_args(call_args) -> list[str]:
    """Extract the argv list from a subprocess.run call (positional arg 0)."""
    return list(call_args[0][0])


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_missing_bblayers_returns_empty(tmp_path: Path) -> None:
    """No bblayers.conf (pre-first-build) yields an empty list, no git calls."""
    cfg = _nxp_cfg(tmp_path)
    assert not cfg.bblayers_conf.is_file()

    with patch("bspctl.layers.subprocess.run") as run:
        result = collect_layer_hashes(cfg)

    assert result == []
    run.assert_not_called()


def test_two_present_repos_return_sorted(tmp_path: Path) -> None:
    """Two repos with present dirs and successful git return two sorted entries."""
    cfg = _nxp_cfg(tmp_path)
    repos = ["poky", "meta-freescale"]
    _write_bblayers(cfg, repos)
    _make_source_dirs(cfg, repos)

    hashes = {"poky": "deadbee", "meta-freescale": "a1b2c3d"}
    branches = {"poky": "scarthgap", "meta-freescale": "lf-6.6.y"}

    def fake_run(argv, **kwargs):
        # argv[2] is the -C path; the repo is its final segment.
        repo = argv[2].rsplit("/", 1)[-1]
        if "rev-parse" in argv:
            return _Completed(0, hashes[repo] + "\n")
        return _Completed(0, branches[repo] + "\n")

    with patch("bspctl.layers.subprocess.run", side_effect=fake_run):
        result = collect_layer_hashes(cfg)

    assert result == [
        LayerHash(repo="meta-freescale", short_hash="a1b2c3d", branch="lf-6.6.y"),
        LayerHash(repo="poky", short_hash="deadbee", branch="scarthgap"),
    ]
    # Sorted by repo name.
    assert [lh.repo for lh in result] == sorted(lh.repo for lh in result)


def test_missing_source_dir_is_omitted(tmp_path: Path) -> None:
    """A repo named in bblayers.conf with no sources/<repo> dir is skipped."""
    cfg = _nxp_cfg(tmp_path)
    _write_bblayers(cfg, ["poky", "ghost"])
    _make_source_dirs(cfg, ["poky"])  # ghost dir intentionally absent

    def fake_run(argv, **kwargs):
        if "rev-parse" in argv:
            return _Completed(0, "deadbee\n")
        return _Completed(0, "scarthgap\n")

    with patch("bspctl.layers.subprocess.run", side_effect=fake_run) as run:
        result = collect_layer_hashes(cfg)

    assert [lh.repo for lh in result] == ["poky"]
    # The missing-dir repo never reaches a git call.
    for call in run.call_args_list:
        assert "/sources/ghost" not in _git_args(call)[2]


def test_rev_parse_failure_omits_repo(tmp_path: Path) -> None:
    """A repo whose rev-parse exits non-zero is dropped from the result."""
    cfg = _nxp_cfg(tmp_path)
    repos = ["poky", "broken"]
    _write_bblayers(cfg, repos)
    _make_source_dirs(cfg, repos)

    def fake_run(argv, **kwargs):
        repo = argv[2].rsplit("/", 1)[-1]
        if "rev-parse" in argv:
            if repo == "broken":
                return _Completed(128, "")
            return _Completed(0, "deadbee\n")
        return _Completed(0, "scarthgap\n")

    with patch("bspctl.layers.subprocess.run", side_effect=fake_run):
        result = collect_layer_hashes(cfg)

    assert [lh.repo for lh in result] == ["poky"]


def test_empty_branch_yields_empty_branch_field(tmp_path: Path) -> None:
    """A detached-HEAD repo (empty branch --show-current) gets an empty branch."""
    cfg = _nxp_cfg(tmp_path)
    _write_bblayers(cfg, ["poky"])
    _make_source_dirs(cfg, ["poky"])

    def fake_run(argv, **kwargs):
        if "rev-parse" in argv:
            return _Completed(0, "deadbee\n")
        return _Completed(0, "\n")  # detached HEAD: no current branch

    with patch("bspctl.layers.subprocess.run", side_effect=fake_run):
        result = collect_layer_hashes(cfg)

    assert result == [LayerHash(repo="poky", short_hash="deadbee", branch="")]
