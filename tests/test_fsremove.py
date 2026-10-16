"""Tests for the shared parallel filesystem-removal primitives in
``bakar.fsremove``.

``parallel_rmtree`` is the build-dir wipe path shared by ``bakar clean``; it
must remove a populated tree completely, no-op on an absent root, never follow
symlinks out of the tree, and choose its parallel-deletion units by descending
into the real fan-out (so a deep ``tmp/work/<arch>/<recipe>`` tree explodes into
many independent subtrees rather than one serial rmtree).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from bakar.fsremove import _gather_remove_targets, parallel_apply, parallel_rmtree

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


def _populate(root: Path, dirs: int, files_per_dir: int) -> None:
    for d in range(dirs):
        sub = root / f"recipe{d}" / "1.0" / "temp"
        sub.mkdir(parents=True)
        for f in range(files_per_dir):
            (sub / f"f{f}").write_text("x")


def test_parallel_rmtree_removes_populated_tree(tmp_path: Path) -> None:
    root = tmp_path / "build"
    _populate(root, dirs=12, files_per_dir=4)
    assert root.exists()

    parallel_rmtree(root)

    assert not root.exists(), "the whole tree must be gone"


def test_parallel_rmtree_noop_on_absent_root(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    # Must not raise.
    parallel_rmtree(missing)
    assert not missing.exists()


def test_parallel_rmtree_returns_none_when_fully_removed(tmp_path: Path) -> None:
    root = tmp_path / "build"
    _populate(root, dirs=3, files_per_dir=2)
    assert parallel_rmtree(root) is None
    assert parallel_rmtree(root) is None, "absent root stays a no-op returning None"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_parallel_rmtree_reports_residue_and_removes_the_rest(tmp_path: Path) -> None:
    root = tmp_path / "build"
    _populate(root, dirs=12, files_per_dir=2)
    locked = root / "locked"
    (locked / "inner").mkdir(parents=True)
    (locked / "inner" / "f").write_text("x")
    locked.chmod(0o500)
    try:
        result = parallel_rmtree(root)
        assert result is not None
        path, exc = result
        assert path.exists()
        assert isinstance(exc, OSError)
        survivors = {p.name for p in root.iterdir()}
        assert survivors == {"locked"}, "every other subtree must be gone"
    finally:
        locked.chmod(0o700)


def test_parallel_rmtree_removes_files_and_sockets_at_top(tmp_path: Path) -> None:
    root = tmp_path / "build"
    root.mkdir()
    (root / "tmp").mkdir()
    (root / "hashserve.sock").write_text("")  # a plain file standing in for a socket
    (root / "tmp" / "work").mkdir()
    (root / "tmp" / "work" / "r0").mkdir()
    (root / "tmp" / "work" / "r0" / "obj").write_text("x")

    parallel_rmtree(root)

    assert not root.exists()


def test_parallel_rmtree_does_not_follow_symlink_out_of_tree(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("precious")

    root = tmp_path / "build"
    root.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)

    parallel_rmtree(root)

    assert not root.exists(), "build tree removed"
    assert (outside / "keep").exists(), "symlink target outside the tree must survive"


def test_gather_remove_targets_expands_the_high_fanout_branch(tmp_path: Path) -> None:
    root = tmp_path / "build"
    # One heavy branch (many recipe dirs) and two light branches.
    work = root / "tmp" / "work"
    work.mkdir(parents=True)
    for d in range(40):
        (work / f"recipe{d}").mkdir()
    (root / "tmp" / "stamps").mkdir()
    (root / "conf").mkdir()

    targets = _gather_remove_targets(root, min_fanout=16)

    # The heavy work/ branch must have been expanded into its per-recipe dirs,
    # so the frontier reaches the requested fan-out instead of staying coarse.
    assert len(targets) >= 16, f"expected fan-out >= 16, got {len(targets)}"
    # No target may be an ancestor of another (the frontier is an antichain, so
    # parallel removal cannot race a parent against its child).
    for a in targets:
        for b in targets:
            if a is not b:
                assert a not in b.parents, f"{a} is an ancestor of {b}"


def _progress_column_types(monkeypatch: pytest.MonkeyPatch, *, show_eta: bool) -> set[str]:
    """Run parallel_apply and return the Rich Progress column type names it built."""
    import rich.progress

    captured: dict[str, tuple] = {}
    real_progress = rich.progress.Progress

    class _SpyProgress(real_progress):
        def __init__(self, *columns, **kwargs) -> None:
            captured["columns"] = columns
            super().__init__(*columns, **kwargs)

    monkeypatch.setattr("rich.progress.Progress", _SpyProgress)
    parallel_apply([1, 2, 3], lambda x: x, "d", show_eta=show_eta)
    return {type(c).__name__ for c in captured["columns"]}


def test_parallel_apply_show_eta_false_drops_time_column(monkeypatch: pytest.MonkeyPatch) -> None:
    types = _progress_column_types(monkeypatch, show_eta=False)
    assert "TimeRemainingColumn" not in types
    assert "BarColumn" in types


def test_parallel_apply_show_eta_true_keeps_time_column(monkeypatch: pytest.MonkeyPatch) -> None:
    types = _progress_column_types(monkeypatch, show_eta=True)
    assert "TimeRemainingColumn" in types


def test_parallel_rmtree_removes_without_eta_column(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The build-dir wipe drops the (inaccurate) ETA and still removes the tree."""
    import bakar.fsremove as fsremove

    root = tmp_path / "build-x"
    _populate(root, dirs=3, files_per_dir=2)

    seen: dict[str, object] = {}
    real_apply = fsremove.parallel_apply

    def _spy(items, fn, description, *, show_eta=True):
        seen["show_eta"] = show_eta
        return real_apply(items, fn, description, show_eta=show_eta)

    monkeypatch.setattr(fsremove, "parallel_apply", _spy)
    parallel_rmtree(root)

    assert seen["show_eta"] is False
    assert not root.exists()


def test_parallel_rmtree_residue_memory_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Undeletable entries are counted but only a capped sample (no tracebacks) is kept."""
    import bakar.fsremove as fsremove

    root = tmp_path / "build"
    _populate(root, dirs=6, files_per_dir=1)
    emitted = 0

    def fake_rmtree(path, onexc=None, **_kw):
        nonlocal emitted
        for i in range(500):
            emitted += 1
            try:
                raise PermissionError(13, "denied")
            except PermissionError as exc:
                onexc(os.unlink, f"{path}/entry{i}", exc)

    monkeypatch.setattr(fsremove.shutil, "rmtree", fake_rmtree)
    result = parallel_rmtree(root)

    assert result is not None
    _path, exc = result
    sample = exc.residue_sample  # ty: ignore[unresolved-attribute]
    assert len(sample) <= fsremove.RESIDUE_SAMPLE_LIMIT
    assert all(e.__traceback__ is None for _p, e in sample)
    assert exc.residue_count > fsremove.RESIDUE_SAMPLE_LIMIT  # ty: ignore[unresolved-attribute]
    # The skeleton pass emitted 500 more on the root; those are not re-recorded.
    assert exc.residue_count == emitted - 500  # ty: ignore[unresolved-attribute]


def _rmtree_leaving_root(root: Path, real_rmtree):
    """Stand-in for ``shutil.rmtree`` whose rmdir of *root* itself fails (EBUSY-like)."""

    def fake(path, onexc=None, **kw):
        if str(path) == str(root):
            real_rmtree_children(root, real_rmtree)
            onexc(os.rmdir, str(path), OSError(16, "Device or resource busy"))
            return
        return real_rmtree(path, onexc=onexc, **kw)

    return fake


def real_rmtree_children(root: Path, real_rmtree) -> None:
    for child in list(root.iterdir()):
        if child.is_dir() and not child.is_symlink():
            real_rmtree(child)
        else:
            child.unlink()


def test_parallel_rmtree_empty_root_that_cannot_be_removed_is_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mount-point root: contents gone, only its own rmdir fails -> success, not residue."""
    import bakar.fsremove as fsremove

    root = tmp_path / "build"
    _populate(root, dirs=3, files_per_dir=2)
    monkeypatch.setattr(fsremove.shutil, "rmtree", _rmtree_leaving_root(root, fsremove.shutil.rmtree))

    assert parallel_rmtree(root) is None
    assert root.is_dir()
    assert list(root.iterdir()) == []


def test_parallel_rmtree_root_failure_with_entries_left_is_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import bakar.fsremove as fsremove

    root = tmp_path / "build"
    _populate(root, dirs=2, files_per_dir=1)
    (root / "stuck").write_text("x")
    real = fsremove.shutil.rmtree
    real_unlink = type(root).unlink

    def fake(path, onexc=None, **kw):
        if str(path) == str(root):
            onexc(os.rmdir, str(path), OSError(16, "busy"))
            return
        return real(path, onexc=onexc, **kw)

    def stuck_unlink(self, *a, **kw):
        if self.name == "stuck":
            raise PermissionError(13, "denied")
        return real_unlink(self, *a, **kw)

    monkeypatch.setattr(fsremove.shutil, "rmtree", fake)
    monkeypatch.setattr(type(root), "unlink", stuck_unlink)
    result = parallel_rmtree(root)
    assert result is not None, "root still holds an entry, so it is residue"
    assert result[0].name == "stuck"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_parallel_rmtree_empty_root_under_readonly_parent_is_success(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    root = parent / "build"
    _populate(root, dirs=3, files_per_dir=1)
    parent.chmod(0o555)
    try:
        assert parallel_rmtree(root) is None
        assert root.is_dir()
        assert list(root.iterdir()) == []
    finally:
        parent.chmod(0o755)
