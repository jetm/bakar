"""``--on`` dispatch must not delete or overwrite the remote node's package feed.

The mirror is an additive ``rsync -a``: it never deletes a remote file, but it
does overwrite one whose name it also carries. A feed lives in the workspace by
default (``<workspace>/_feed`` and its ``-stage`` sibling), it is built up over
many syncs, and it is not reproducible from the local tree - so the excludes
keep the mirror away from it entirely.

The tests run a REAL rsync between two directories rather than asserting on
argv. An exclude pattern that is present but wrong - unanchored where it should
be anchored, matching a file but not a directory - still shows up in the argv a
string assertion checks. Only running the command distinguishes those.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import TYPE_CHECKING

import pytest

from bakar.steps.remote_dispatch import build_rsync_argv

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


def _local_rsync_argv(src: Path, dst: Path) -> list[str]:
    """Return the dispatch argv with the remote destination rewritten as local.

    Keeps every exclude the real dispatch uses; only the ``host:`` prefix is
    dropped so the command can run without ssh. Built from build_rsync_argv
    rather than hand-assembled, so a change to the exclude list is exercised
    here rather than silently bypassed.
    """
    argv = build_rsync_argv(src, "unused-host")
    return [f"{dst}/" if arg.startswith("unused-host:") else arg for arg in argv]


@pytest.fixture
def workspaces(tmp_path: Path) -> tuple[Path, Path]:
    """A local workspace with sources, and a remote one that also has a feed."""
    local = tmp_path / "local"
    remote = tmp_path / "remote"
    (local / "meta-avocado" / "kas").mkdir(parents=True)
    (local / "meta-avocado" / "kas" / "machine.yml").write_text("header: {}\n")
    (remote / "meta-avocado" / "kas").mkdir(parents=True)

    # A feed the remote accumulated, with the pool and a snapshot under it - the
    # shape `bakar feed sync` leaves behind, not just a marker file.
    pool = remote / "_feed" / "dev" / "local" / "_pkgs" / "ab"
    pool.mkdir(parents=True)
    (pool / "abc123.rpm").write_bytes(b"pkg")
    snapshot = remote / "_feed" / "dev" / "local" / "snapshots" / "20260101T000000Z"
    snapshot.mkdir(parents=True)
    (snapshot / "repomd.xml").write_text("<repomd/>")
    stage = remote / "_feed-stage" / "dev" / "local" / "target" / "qemux86-64"
    stage.mkdir(parents=True)
    (stage / "staged.rpm").write_bytes(b"pkg")
    return local, remote


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync not installed")
def test_dispatch_mirror_preserves_a_remote_feed(workspaces) -> None:
    """The feed and its stage survive a mirror that does not carry them."""
    local, remote = workspaces

    result = subprocess.run(_local_rsync_argv(local, remote), capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr

    assert (remote / "_feed" / "dev" / "local" / "_pkgs" / "ab" / "abc123.rpm").is_file()
    assert (remote / "_feed" / "dev" / "local" / "snapshots" / "20260101T000000Z" / "repomd.xml").is_file()
    assert (remote / "_feed-stage" / "dev" / "local" / "target" / "qemux86-64" / "staged.rpm").is_file()


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync not installed")
def test_dispatch_mirror_never_deletes_a_stale_remote_file(workspaces) -> None:
    """A remote file with no local counterpart survives: the mirror is additive.

    The deleting mirror removed 12,961 such files from one node in a single
    preview; dispatch must leave every one of them in place.
    """
    local, remote = workspaces
    stale = remote / "meta-avocado" / "kas" / "deleted-upstream.yml"
    stale.write_text("header: {}\n")

    result = subprocess.run(_local_rsync_argv(local, remote), capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr

    assert stale.read_text() == "header: {}\n"
    assert (remote / "meta-avocado" / "kas" / "machine.yml").is_file()


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync not installed")
def test_a_symlinked_feed_survives_the_mirror(tmp_path: Path) -> None:
    """A feed is routinely a symlink onto a storage volume, not a real directory.

    That is how it is set up on the two-node cluster. The symlink, and the
    storage behind it, must survive a mirror from a side that has no feed.
    """
    local = tmp_path / "local"
    remote = tmp_path / "remote"
    storage = tmp_path / "storage"
    (local / "src").mkdir(parents=True)
    (local / "src" / "a.txt").write_text("x\n")
    remote.mkdir()
    (storage / "dev" / "local").mkdir(parents=True)
    (storage / "dev" / "local" / "pool.rpm").write_bytes(b"pkg")
    (remote / "_feed").symlink_to(storage)
    (remote / "_feed-stage").symlink_to(storage)

    subprocess.run(_local_rsync_argv(local, remote), capture_output=True, text=True, check=False)

    assert (remote / "_feed").is_symlink()
    assert (remote / "_feed-stage").is_symlink()
    assert (storage / "dev" / "local" / "pool.rpm").is_file()


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync not installed")
def test_a_source_dir_merely_named_like_the_feed_is_still_mirrored(tmp_path: Path) -> None:
    """The exclude is anchored at the workspace root, not matched at any depth.

    A layer that happens to ship a directory called ``_feed`` deeper in the tree
    is source and must mirror. An unanchored pattern would silently stop
    delivering it, which is the mirror-image failure of the one being fixed.
    """
    local = tmp_path / "local"
    remote = tmp_path / "remote"
    nested = local / "meta-openembedded" / "recipes-core" / "_feed"
    nested.mkdir(parents=True)
    (nested / "source.bb").write_text("SUMMARY = 'x'\n")
    remote.mkdir()

    subprocess.run(_local_rsync_argv(local, remote), capture_output=True, text=True, check=False)

    assert (remote / "meta-openembedded" / "recipes-core" / "_feed" / "source.bb").is_file()


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync not installed")
def test_a_local_edit_is_carried_and_caches_stay_excluded(workspaces) -> None:
    """Additive does not mean stale: an edited local file overwrites the remote copy."""
    local, remote = workspaces
    (remote / "meta-avocado" / "kas" / "machine.yml").write_text("header: {old: 1}\n")
    # A different size, so rsync's size+mtime quick check cannot skip the file.
    (local / "meta-avocado" / "kas" / "machine.yml").write_text("header: {newer: 1}\n")
    (local / "layer" / "tmp").mkdir(parents=True)
    (local / "layer" / "tmp" / "junk").write_text("x\n")

    result = subprocess.run(_local_rsync_argv(local, remote), capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr

    assert (remote / "meta-avocado" / "kas" / "machine.yml").read_text() == "header: {newer: 1}\n"
    assert not (remote / "layer" / "tmp").exists()
