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
from tests.conftest import make_host_profile


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


def test_default_dirs_are_the_configured_env_paths(monkeypatch) -> None:
    """With no override, the targets are the exported SSTATE_DIR / DL_DIR paths -
    exactly the directories the cache-dirs check inspects, so the remediation can
    clear it."""
    monkeypatch.setenv("SSTATE_DIR", "/mnt/build/sstate")
    monkeypatch.setenv("DL_DIR", "/mnt/build/downloads")
    action = CacheDirsAction()
    assert action.dirs == [Path("/mnt/build/sstate"), Path("/mnt/build/downloads")]


def test_default_dirs_empty_when_env_unset(monkeypatch) -> None:
    """When neither SSTATE_DIR nor DL_DIR is set, there is nothing to create: the
    cache-dirs check passes in that case, so the action is trivially satisfied and
    yields no operation."""
    monkeypatch.delenv("SSTATE_DIR", raising=False)
    monkeypatch.delenv("DL_DIR", raising=False)
    action = CacheDirsAction()
    assert action.dirs == []
    assert action.operations() == []
    assert action.is_satisfied(make_host_profile()) is True


def test_is_satisfied_true_for_existing_writable_dirs(tmp_path) -> None:
    dirs = [tmp_path / "sstate", tmp_path / "downloads", tmp_path / "ccache"]
    for d in dirs:
        d.mkdir()
    assert CacheDirsAction(dirs).is_satisfied(make_host_profile()) is True


def test_is_satisfied_false_when_a_dir_is_missing(tmp_path) -> None:
    present = tmp_path / "sstate"
    present.mkdir()
    missing = tmp_path / "downloads"  # never created
    assert CacheDirsAction([present, missing]).is_satisfied(make_host_profile()) is False


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
            assert CacheDirsAction([writable, readonly]).is_satisfied(make_host_profile()) is False
    finally:
        os.chmod(readonly, 0o700)
