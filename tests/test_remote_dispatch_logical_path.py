"""Tests that remote dispatch addresses the remote by a path that exists there.

``--on`` mirrors the working tree to the remote at the SAME absolute path and
then ``cd``s to it, so both paths have to mean the same directory on both nodes.
``os.getcwd()`` and ``Path.resolve()`` flatten symlinks, which breaks exactly
that: this cluster mounts its shared workspace at a home-relative path on every
node, and on the node that owns the storage that path is a symlink onto the
storage volume. Resolving it produces a path that exists on precisely one
machine, and the dispatch fails with rsync's ``mkdir ... No such file or
directory`` after previewing 50,454 files.

The symlink is the recent part, not the resolving. ``detect_kas_workspace`` has
resolved since 2026-04-13; the workspace became a symlink on 2026-08-07, which
is when a correct-looking resolve started producing an unusable remote path.
"""

from __future__ import annotations

import os

import pytest

from bakar.commands._helpers import logical_path

pytestmark = pytest.mark.unit


def test_a_symlinked_workspace_keeps_its_logical_path(tmp_path, monkeypatch) -> None:
    """The path the user stood in is preferred over its resolved target.

    This is the cluster's actual shape: ``~/yocto-cache`` is a symlink onto the
    storage volume on the owning node, and every other node mounts the share at
    the home-relative path. Only the logical form exists on both.
    """
    storage = tmp_path / "mnt" / "STORAGE" / "shared-workspace"
    storage.mkdir(parents=True)
    home_view = tmp_path / "home" / "yocto-cache"
    home_view.parent.mkdir(parents=True)
    os.symlink(tmp_path / "mnt" / "STORAGE", home_view)
    logical = home_view / "shared-workspace"

    monkeypatch.setenv("PWD", str(logical))

    assert logical_path(storage) == logical


def test_a_subdirectory_under_the_symlink_is_mapped_too(tmp_path, monkeypatch) -> None:
    """A path below the logical cwd keeps the logical prefix.

    The workspace root and the invoking cwd are not always the same directory -
    a build is often dispatched from a machine subdirectory - so mapping only an
    exact match would leave the other one physical.
    """
    storage = tmp_path / "mnt" / "STORAGE" / "ws"
    (storage / "build-qemux86-64").mkdir(parents=True)
    home_view = tmp_path / "home" / "cache"
    home_view.parent.mkdir(parents=True)
    os.symlink(tmp_path / "mnt" / "STORAGE", home_view)

    monkeypatch.setenv("PWD", str(home_view / "ws"))

    assert logical_path(storage / "build-qemux86-64") == home_view / "ws" / "build-qemux86-64"


def test_an_unrelated_path_is_returned_unchanged(tmp_path, monkeypatch) -> None:
    """A path outside the logical cwd is not rewritten.

    Rewriting it would invent a path that does not exist anywhere, which is
    worse than the physical one that at least exists locally.
    """
    monkeypatch.setenv("PWD", str(tmp_path / "somewhere"))
    other = tmp_path / "elsewhere" / "tree"

    assert logical_path(other) == other


def test_no_pwd_in_the_environment_returns_the_physical_path(tmp_path, monkeypatch) -> None:
    """Without PWD there is no logical view to prefer, so nothing is guessed."""
    monkeypatch.delenv("PWD", raising=False)

    assert logical_path(tmp_path / "tree") == tmp_path / "tree"


def test_a_relative_pwd_is_ignored(tmp_path, monkeypatch) -> None:
    """A relative PWD cannot anchor an absolute path and is not trusted.

    PWD is shell-maintained and a caller can set it to anything; a relative
    value means it is not describing a real logical cwd.
    """
    monkeypatch.setenv("PWD", "relative/path")

    assert logical_path(tmp_path / "tree") == tmp_path / "tree"


def test_a_stale_pwd_that_no_longer_resolves_is_ignored(tmp_path, monkeypatch) -> None:
    """A PWD naming a deleted directory does not rewrite anything.

    PWD survives a directory being removed underneath the shell, so it can name
    a path that no longer exists - and trusting it then would map a live path
    onto a dead prefix.
    """
    monkeypatch.setenv("PWD", str(tmp_path / "deleted"))
    live = tmp_path / "real" / "tree"
    live.mkdir(parents=True)

    assert logical_path(live) == live


def test_an_unsymlinked_workspace_is_unaffected(tmp_path, monkeypatch) -> None:
    """Where the logical and physical paths already agree, nothing changes.

    This is the secondary node's shape - the share is a real mount at the
    home-relative path - so the mapping has to be a no-op there rather than
    producing a second spelling of the same directory.
    """
    ws = tmp_path / "home" / "cache" / "ws"
    ws.mkdir(parents=True)

    monkeypatch.setenv("PWD", str(ws))

    assert logical_path(ws) == ws
