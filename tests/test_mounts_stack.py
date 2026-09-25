"""Tests for mount-stack resolution in ``bakar.mounts``.

Covers picking the last-listed entry among mountpoint ties (the filesystem
the kernel currently resolves through when something is mounted over an
existing trap), and the autofs-is-undetermined tri-state in
:func:`bakar.mounts.is_path_on_nfs`.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from bakar.mounts import _mount_entry_in, is_path_on_nfs

pytestmark = pytest.mark.unit


# Verbatim PC3 `/proc/mounts` lines: an nfs4 share mounted over its systemd
# autofs trap at the same mountpoint. autofs is listed first (it's the
# long-lived trap systemd created at boot); nfs4 is listed second (the kernel
# applied it later, on first access), which is why the kernel resolves the
# path through nfs4 despite autofs appearing earlier in the table.
_AUTOFS_LINE = (
    "systemd-1 /home/tiamarin/yocto-cache/ccache autofs "
    "rw,relatime,fd=69,pgrp=1,timeout=600,minproto=5,maxproto=5,direct,pipe_ino=10747 0 0"
)
_NFS4_LINE = (
    "192.168.8.174:/mnt/YOCTO_CACHE/ccache /home/tiamarin/yocto-cache/ccache nfs4 "
    "rw,relatime,vers=4.2,rsize=1048576,wsize=1048576,namlen=255,hard,proto=tcp,nconnect=8,"
    "timeo=600,retrans=2,sec=sys,clientaddr=192.168.8.187,local_lock=none,addr=192.168.8.174 0 0"
)
_STACKED_MOUNTS = f"{_AUTOFS_LINE}\n{_NFS4_LINE}\n"

_CCACHE_PATH = Path("/home/tiamarin/yocto-cache/ccache")


def _patch_proc_mounts(monkeypatch: pytest.MonkeyPatch, content: str) -> None:
    """Patch ``Path.read_text`` so reads of ``/proc/mounts`` return ``content``.

    Other paths keep their real ``read_text`` behavior, matching the helper of
    the same name in ``tests/test_diagnostics.py``.
    """
    real_read_text = Path.read_text

    def fake_read_text(self: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if str(self) == "/proc/mounts":
            return content
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read_text)


def _write_stub_realpath_identity(tmp_path: Path) -> Path:
    """Executable ``realpath`` stub that echoes its argument back unresolved.

    ``_mount_entry_in``/``is_path_on_nfs`` now resolve symlinks by forking a
    real ``realpath`` child (see ``bakar.mounts._resolve_bounded``), not by
    calling ``Path.resolve()`` in this process - so monkeypatching
    ``Path.resolve`` (the old mechanism this fixture used) no longer has any
    effect on them; a stub aimed at that method would silently stop being
    exercised while still reading as though it took.

    ``_CCACHE_PATH`` names a real path under this developer's home directory
    that on this dev machine happens to be a symlink
    (``~/yocto-cache -> /mnt/YOCTO_CACHE``) unrelated to the fixture under
    test. A real resolution would follow it and silently stop matching the
    literal mountpoint text in the PC3 fixture lines, so this stub identity-
    echoes the already-absolute, already-normalized test literal instead of
    resolving anything - the same "no symlinks followed" behavior the old
    ``Path.resolve`` patch gave, applied at the actual mechanism this project
    now uses.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "realpath"
    stub.write_text("#!/bin/sh\nshift\nprintf '%s\\n' \"$1\"\n")
    stub.chmod(0o755)
    return bin_dir


def _patch_resolve_no_symlinks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Prepend the identity ``realpath`` stub onto ``PATH`` and return its bin dir.

    Returns the bin dir so a caller can assert the stub was actually invoked
    (a call-count file), proving the bounded-resolution mechanism is what ran
    rather than a dead patch nobody exercises.
    """
    bin_dir = _write_stub_realpath_identity(tmp_path)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return bin_dir


def test_mount_entry_in_stacked_autofs_then_nfs4_resolves_to_nfs4(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The nfs4 entry wins - it is last-listed at the shared mountpoint, not
    the first-listed autofs entry a stable length-only sort would return."""
    _patch_resolve_no_symlinks(monkeypatch, tmp_path)
    entry = _mount_entry_in(_STACKED_MOUNTS, _CCACHE_PATH)
    assert entry is not None
    assert entry[2] == "nfs4"
    assert entry[0] == "192.168.8.174:/mnt/YOCTO_CACHE/ccache"


def test_is_path_on_nfs_stacked_autofs_then_nfs4_is_true(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_resolve_no_symlinks(monkeypatch, tmp_path)
    _patch_proc_mounts(monkeypatch, _STACKED_MOUNTS)
    assert is_path_on_nfs(_CCACHE_PATH) is True


def test_is_path_on_nfs_autofs_alone_is_none(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A bare autofs trap with nothing mounted over it is undetermined, not
    False - autofs names no real filesystem, so it cannot confirm local."""
    _patch_resolve_no_symlinks(monkeypatch, tmp_path)
    _patch_proc_mounts(monkeypatch, f"{_AUTOFS_LINE}\n")
    assert is_path_on_nfs(_CCACHE_PATH) is None


def test_is_path_on_nfs_ext4_is_false(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A confirmed-local ext4 mount -> False."""
    target = tmp_path / "ws"
    target.mkdir()
    _patch_proc_mounts(monkeypatch, f"/dev/sda1 {target.resolve()} ext4 rw 0 0\n")
    assert is_path_on_nfs(target) is False


def test_mount_entry_in_root_and_home_resolves_to_home() -> None:
    """The longest covering prefix wins: ``/home`` over ``/``."""
    table = "rootfs / ext4 rw 0 0\n/dev/sda2 /home ext4 rw 0 0\n"
    entry = _mount_entry_in(table, Path("/home/someuser/project"))
    assert entry is not None
    assert entry[1] == "/home"


def test_mount_entry_in_share_does_not_cover_sibling_prefix() -> None:
    """``/mnt/share`` must not match ``/mnt/shared-other/x`` - covering is
    component-wise (``is_relative_to``), not a string prefix test."""
    table = "srv:/x /mnt/share nfs4 rw 0 0\n"
    entry = _mount_entry_in(table, Path("/mnt/shared-other/x"))
    assert entry is None


def test_mount_entry_in_two_entries_same_mountpoint_resolves_to_last() -> None:
    """Two arbitrary entries stacked at one mountpoint - the last-listed one
    wins, independent of the nfs4/autofs case above."""
    table = "first-src /mnt/x ext4 rw 0 0\nsecond-src /mnt/x tmpfs rw 0 0\n"
    entry = _mount_entry_in(table, Path("/mnt/x"))
    assert entry is not None
    assert entry[0] == "second-src"
    assert entry[2] == "tmpfs"
