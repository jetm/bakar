"""``--on`` dispatch must not delete the remote node's package feed.

The mirror is ``rsync -a --delete``, so anything present on the remote and
absent locally is removed before the build starts. A feed lives in the
workspace by default (``<workspace>/_feed`` and its ``-stage`` sibling), it is
built up over many syncs, and it is not reproducible from the local tree - so
deleting it costs every retained snapshot and the content pool behind them.

The central test runs a REAL rsync between two directories rather than
asserting on argv. An exclude pattern that is present but wrong - unanchored
where it should be anchored, missing a trailing slash, matching a file but not
a directory - still shows up in the argv a string assertion checks, and still
deletes the feed. Only running the command distinguishes those.
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
    """The feed and its stage survive a --delete mirror that does not carry them."""
    local, remote = workspaces

    result = subprocess.run(_local_rsync_argv(local, remote), capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr

    assert (remote / "_feed" / "dev" / "local" / "_pkgs" / "ab" / "abc123.rpm").is_file()
    assert (remote / "_feed" / "dev" / "local" / "snapshots" / "20260101T000000Z" / "repomd.xml").is_file()
    assert (remote / "_feed-stage" / "dev" / "local" / "target" / "qemux86-64" / "staged.rpm").is_file()


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync not installed")
def test_dispatch_mirror_still_deletes_stale_remote_sources(workspaces) -> None:
    """The exclusion must not turn --delete off for everything else.

    A test that only asserts the feed survives also passes when --delete was
    dropped entirely, which would leave removed source files live on the remote.
    """
    local, remote = workspaces
    stale = remote / "meta-avocado" / "kas" / "deleted-upstream.yml"
    stale.write_text("header: {}\n")

    subprocess.run(_local_rsync_argv(local, remote), capture_output=True, text=True, check=False)

    assert not stale.exists()
    assert (remote / "meta-avocado" / "kas" / "machine.yml").is_file()


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync not installed")
def test_a_symlinked_feed_survives_the_mirror(tmp_path: Path) -> None:
    """A feed is routinely a symlink onto a storage volume, not a real directory.

    That is how it is set up on the two-node cluster, and it is the case a
    directory-only exclude pattern silently fails to protect: measured, a
    trailing-slash `/_feed/` deletes the symlink while a real directory under the
    same pattern survives. Testing only the directory shape would pass against a
    fix that does not work anywhere it is actually deployed.
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
