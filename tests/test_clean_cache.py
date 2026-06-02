"""Tests for the two-phase staging deletion in ``_delete_stale`` / ``_stage_and_delete``.

Covers the reshaped helpers introduced in task 3.2: ``_delete_stale`` now delegates
file removal to ``_stage_and_delete`` (move-then-rmtree) instead of calling
``f.unlink()`` directly.  These unit tests verify:

- ``_stage_and_delete`` moves files into a ``.bakar-gc-*`` staging dir inside the
  sstate root, then removes the staging dir, returning ``(removed, freed)``.
- ``_delete_stale`` returns the correct ``(removed, freed, empty_dirs)`` tuple and
  prunes emptied directories.
- The ``(removed, freed, empty_dirs)`` return shape consumed by the ``clean_cache``
  command is preserved.
"""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING

import pytest

from bakar.commands.clean_cache import _delete_stale, _stage_and_delete

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _age_file(path: Path, days_old: float) -> None:
    ts = time.time() - days_old * 86400
    os.utime(path, (ts, ts))


def _make_files(root: Path, count: int, content: bytes = b"x") -> list[Path]:
    root.mkdir(parents=True, exist_ok=True)
    files = []
    for i in range(count):
        p = root / f"file_{i}.tar.zst"
        p.write_bytes(content + str(i).encode())
        files.append(p)
    return files


# ---------------------------------------------------------------------------
# _stage_and_delete
# ---------------------------------------------------------------------------


def test_stage_and_delete_removes_files(tmp_path: Path) -> None:
    """All stale files are gone after _stage_and_delete completes."""
    sstate = tmp_path / "sstate"
    files = _make_files(sstate, 3, content=b"payload")

    removed, _freed = _stage_and_delete(files, sstate)

    assert removed == 3
    for f in files:
        assert not f.exists(), f"{f} should have been removed"


def test_stage_and_delete_returns_correct_freed_bytes(tmp_path: Path) -> None:
    """freed equals the sum of pre-move file sizes."""
    sstate = tmp_path / "sstate"
    sstate.mkdir()
    content = b"A" * 100
    files = []
    for i in range(4):
        p = sstate / f"f{i}.zst"
        p.write_bytes(content)
        files.append(p)

    removed, freed = _stage_and_delete(files, sstate)

    assert removed == 4
    assert freed == 400


def test_stage_and_delete_staging_dir_is_inside_sstate_root(tmp_path: Path) -> None:
    """Staging dir is created as a direct child of effective_dir (same device)."""
    sstate = tmp_path / "sstate"
    sstate.mkdir()
    p = sstate / "f.zst"
    p.write_bytes(b"data")

    # Patch os.rename to capture the destination before completing
    original_rename = os.rename
    staging_parents: list[Path] = []

    def capturing_rename(src, dst):
        from pathlib import Path as _Path

        staging_parents.append(_Path(dst).parent)
        original_rename(src, dst)

    import bakar.commands.clean_cache as cc_mod

    original = cc_mod.os.rename
    cc_mod.os.rename = capturing_rename
    try:
        _stage_and_delete([p], sstate)
    finally:
        cc_mod.os.rename = original

    assert len(staging_parents) == 1
    assert staging_parents[0].parent == sstate, (
        f"staging dir {staging_parents[0]} is not a direct child of sstate {sstate}"
    )


def test_stage_and_delete_no_staging_dir_left_behind(tmp_path: Path) -> None:
    """No .bakar-gc-* directory remains after a successful run."""
    sstate = tmp_path / "sstate"
    files = _make_files(sstate, 2)

    _stage_and_delete(files, sstate)

    gc_dirs = list(sstate.glob(".bakar-gc-*"))
    assert gc_dirs == [], f"staging dirs still present: {gc_dirs}"


def test_stage_and_delete_collision_safe_same_basename(tmp_path: Path) -> None:
    """Two files with the same basename in different subdirs are both removed."""
    sstate = tmp_path / "sstate"
    sub_a = sstate / "a"
    sub_b = sstate / "b"
    sub_a.mkdir(parents=True)
    sub_b.mkdir(parents=True)
    fa = sub_a / "same.zst"
    fb = sub_b / "same.zst"
    fa.write_bytes(b"A" * 50)
    fb.write_bytes(b"B" * 50)

    removed, freed = _stage_and_delete([fa, fb], sstate)

    assert removed == 2
    assert freed == 100
    assert not fa.exists()
    assert not fb.exists()


def test_stage_and_delete_skips_missing_files_gracefully(tmp_path: Path) -> None:
    """An OSError on a stale file (e.g. already gone) is skipped, not fatal."""
    sstate = tmp_path / "sstate"
    sstate.mkdir()
    present = sstate / "present.zst"
    present.write_bytes(b"x" * 20)
    ghost = sstate / "ghost.zst"  # never created

    removed, freed = _stage_and_delete([present, ghost], sstate)

    assert removed == 1
    assert freed == 20
    assert not present.exists()


# ---------------------------------------------------------------------------
# _delete_stale
# ---------------------------------------------------------------------------


def test_delete_stale_return_shape(tmp_path: Path) -> None:
    """``_delete_stale`` returns a 3-tuple ``(removed, freed, empty_dirs)``."""
    sstate = tmp_path / "sstate"
    files = _make_files(sstate / "sub", 2, content=b"X" * 10)

    result = _delete_stale(files, sstate)

    assert isinstance(result, tuple)
    assert len(result) == 3
    removed, freed, _empty_dirs = result
    assert removed == 2
    # Each file contains b"X" * 10 + str(i).encode() - sizes are 11 bytes each (i=0 and i=1)
    assert freed == 22


def test_delete_stale_prunes_empty_dirs(tmp_path: Path) -> None:
    """Directories emptied by deletion are removed by _delete_stale."""
    sstate = tmp_path / "sstate"
    sub = sstate / "subdir"
    files = _make_files(sub, 1)

    removed, _freed, empty_dirs = _delete_stale(files, sstate)

    assert removed == 1
    assert empty_dirs >= 1
    assert not sub.exists(), "emptied subdir should have been pruned"


def test_delete_stale_non_empty_dirs_preserved(tmp_path: Path) -> None:
    """Directories still containing files are not removed."""
    sstate = tmp_path / "sstate"
    sub = sstate / "subdir"
    sub.mkdir(parents=True)
    stale = sub / "stale.zst"
    stale.write_bytes(b"stale")
    keeper = sub / "keep.zst"
    keeper.write_bytes(b"keep")

    removed, _freed, _empty_dirs = _delete_stale([stale], sstate)

    assert removed == 1
    assert sub.exists(), "non-empty subdir should be preserved"
    assert keeper.exists(), "keeper file should not be touched"


def test_delete_stale_empty_list_returns_zeros(tmp_path: Path) -> None:
    """Empty stale list: no files deleted, no dirs pruned, all zeros."""
    sstate = tmp_path / "sstate"
    sstate.mkdir()

    removed, freed, empty_dirs = _delete_stale([], sstate)

    assert (removed, freed, empty_dirs) == (0, 0, 0)


def test_delete_stale_freed_bytes_matches_file_sizes(tmp_path: Path) -> None:
    """freed bytes equals the exact pre-deletion size of the stale files."""
    sstate = tmp_path / "sstate"
    sub = sstate / "bucket"
    sub.mkdir(parents=True)
    sizes = [111, 222, 333]
    files = []
    for i, sz in enumerate(sizes):
        p = sub / f"f{i}.zst"
        p.write_bytes(b"Z" * sz)
        files.append(p)

    _, freed, _ = _delete_stale(files, sstate)

    assert freed == sum(sizes)
