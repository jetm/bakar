"""Tests for the shared parallel filesystem-removal primitives in
``bakar.fsremove``.

``parallel_rmtree`` is the build-dir wipe path shared by ``bakar clean``; it
must remove a populated tree completely, no-op on an absent root, never follow
symlinks out of the tree, and choose its parallel-deletion units by descending
into the real fan-out (so a deep ``tmp/work/<arch>/<recipe>`` tree explodes into
many independent subtrees rather than one serial rmtree).
"""

from __future__ import annotations

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
