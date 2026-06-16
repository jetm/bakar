"""Tests for the cache-directory action of ``bakar setup``.

Covers ``CacheDirsAction``: it remediates ``cache-dirs`` unprivileged (never
``needs_root``, never sudo), its single op is a ``mkdir -p`` of the targets,
and ``is_satisfied`` reflects the live filesystem - True only when every
target dir exists and is writable.
"""

from __future__ import annotations

import os
from pathlib import Path

from bakar.setup.actions.base import Action, RunCommand
from bakar.setup.actions.cache import CacheDirsAction
from bakar.setup.profile import HostProfile


def _profile() -> HostProfile:
    """A minimal stand-in profile; this action ignores every field."""
    return HostProfile(
        cpu_count=4,
        mem_available_gb=16.0,
        disk_free_gb=200.0,
        distro_id="arch",
        pkg_manager="pacman",
        in_docker_group=True,
        docker_installed=True,
        inotify_instances=8192,
        inotify_watches=1048576,
        swappiness=10,
        docker_nofile_soft=65536,
    )


def test_cache_action_is_an_action_remediating_cache_dirs() -> None:
    action = CacheDirsAction([Path("/tmp/x")])
    assert isinstance(action, Action)
    assert action.check_name == "cache-dirs"


def test_cache_action_is_unprivileged() -> None:
    """A $HOME mkdir never needs root, and no op is privileged."""
    action = CacheDirsAction([Path("/home/u/.cache/bakar/sstate")])
    assert action.needs_root is False
    assert not any(op.needs_root for op in action.operations())


def test_operation_is_a_single_mkdir_p_never_sudo() -> None:
    dirs = [Path("/home/u/.cache/bakar/sstate"), Path("/home/u/.cache/bakar/dl")]
    ops = CacheDirsAction(dirs).operations()
    assert ops == [
        RunCommand(argv=["mkdir", "-p", str(dirs[0]), str(dirs[1])], needs_root=False),
    ]
    assert "sudo" not in ops[0].argv


def test_default_dirs_live_under_home_cache(monkeypatch) -> None:
    """With no override, the targets are the XDG-cache-home sstate/dl/ccache."""
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: Path("/home/u")))
    action = CacheDirsAction()
    assert action.dirs == [
        Path("/home/u/.cache/bakar/sstate"),
        Path("/home/u/.cache/bakar/downloads"),
        Path("/home/u/.cache/bakar/ccache"),
    ]


def test_default_dirs_honour_xdg_cache_home(monkeypatch) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", "/xdg/cache")
    action = CacheDirsAction()
    assert action.dirs == [
        Path("/xdg/cache/bakar/sstate"),
        Path("/xdg/cache/bakar/downloads"),
        Path("/xdg/cache/bakar/ccache"),
    ]


def test_is_satisfied_true_for_existing_writable_dirs(tmp_path) -> None:
    dirs = [tmp_path / "sstate", tmp_path / "downloads", tmp_path / "ccache"]
    for d in dirs:
        d.mkdir()
    assert CacheDirsAction(dirs).is_satisfied(_profile()) is True


def test_is_satisfied_false_when_a_dir_is_missing(tmp_path) -> None:
    present = tmp_path / "sstate"
    present.mkdir()
    missing = tmp_path / "downloads"  # never created
    assert CacheDirsAction([present, missing]).is_satisfied(_profile()) is False


def test_is_satisfied_false_when_a_dir_is_not_writable(tmp_path) -> None:
    writable = tmp_path / "sstate"
    writable.mkdir()
    readonly = tmp_path / "ccache"
    readonly.mkdir()
    os.chmod(readonly, 0o500)
    try:
        # A root test runner ignores mode bits and stays writable; skip the
        # assertion in that case rather than emit a false failure.
        if os.access(readonly, os.W_OK):
            return
        assert CacheDirsAction([writable, readonly]).is_satisfied(_profile()) is False
    finally:
        os.chmod(readonly, 0o700)
