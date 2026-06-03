"""Tests for the bbsetup ``layers/`` resolution in collect_layer_hashes.

Unlike test_layer_hashes.py (which mocks git), these tests create real git
repos under a bbsetup workspace's ``layers/`` dir and assert the collector
resolves a non-empty result with a real short hash per layer. This validates
design assumption A1: the explicit ``cfg.bsp_root/layers/<repo>`` strategy
yields a non-empty table for a synced bitbake-setup workspace.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

from bakar.config import resolve
from bakar.layers import collect_layer_hashes

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


def _git_repo(path: Path) -> None:
    """Create a git repo at ``path`` with a single commit."""
    path.mkdir(parents=True, exist_ok=True)
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True, env={**env})
    (path / "README").write_text("x\n")
    subprocess.run(["git", "-C", str(path), "add", "README"], check=True, env={**env})
    subprocess.run(
        ["git", "-C", str(path), "commit", "-q", "-m", "init"],
        check=True,
        env={**env},
    )


def _write_bbsetup_bblayers(cfg, repos: list[str]) -> None:
    """Write a bblayers.conf using the ${TOPDIR}/../layers/<repo> layout."""
    conf = cfg.bblayers_conf
    conf.parent.mkdir(parents=True, exist_ok=True)
    lines = ['BBLAYERS ?= " \\']
    lines.extend(f"  ${{TOPDIR}}/../layers/{repo}/meta-{repo} \\" for repo in repos)
    lines.append('"')
    conf.write_text("\n".join(lines) + "\n")


def test_bbsetup_layers_resolve_non_empty(tmp_path: Path) -> None:
    """A bbsetup workspace with layers/<repo> git repos yields a real table."""
    cfg = resolve(workspace=tmp_path, bsp_family="bbsetup")
    repos = ["poky", "meta-openembedded"]
    _write_bbsetup_bblayers(cfg, repos)
    for repo in repos:
        _git_repo(cfg.bsp_root / "layers" / repo)

    result = collect_layer_hashes(cfg)

    resolved = {lh.repo: lh for lh in result if lh.repo in repos}
    assert set(resolved) == set(repos)
    for lh in resolved.values():
        assert lh.short_hash
        assert len(lh.short_hash) >= 7


def test_bbsetup_missing_layer_dir_omitted(tmp_path: Path) -> None:
    """A repo named in bblayers.conf with no layers/<repo> dir is skipped."""
    cfg = resolve(workspace=tmp_path, bsp_family="bbsetup")
    _write_bbsetup_bblayers(cfg, ["poky", "ghost"])
    _git_repo(cfg.bsp_root / "layers" / "poky")  # ghost dir intentionally absent

    result = collect_layer_hashes(cfg)

    assert "poky" in {lh.repo for lh in result}
    assert "ghost" not in {lh.repo for lh in result}


def test_no_bblayers_returns_empty(tmp_path: Path) -> None:
    """A workspace with no bblayers.conf returns [] without raising."""
    cfg = resolve(workspace=tmp_path, bsp_family="bbsetup")
    assert not cfg.bblayers_conf.is_file()

    assert collect_layer_hashes(cfg) == []
