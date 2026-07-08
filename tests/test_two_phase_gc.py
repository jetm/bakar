"""Tests for the two-phase sstate GC introduced in task 3.1/3.2.

Covers ``_stage_and_delete`` unit behaviour and the ``clean-cache`` CLI path:

(a) ``_stage_and_delete`` moves stale files into a ``.bakar-gc-*`` staging dir
    inside the sstate root, then removes both the files and the staging dir,
    returning the correct ``(removed, freed)`` pair.
(b) The staging dir is created as a direct child of the sstate root.
(c) Two stale files with the same basename in different subdirs are both
    removed (collision-safe integer naming).
(d) ``clean-cache --dry-run`` moves and deletes nothing.
(e) End-to-end ``clean-cache --yes --no-ccache`` reports freed bytes and
    leaves no staging dir behind on success.

Old mtimes are set via ``os.utime`` so tests do not depend on wall-clock time
or filesystem atime behaviour.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from bakar.cli import app
from bakar.commands.clean_cache import _stage_and_delete

pytestmark = pytest.mark.unit

runner = CliRunner()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _age_file(path: Path, days_old: float) -> None:
    """Backdate *path* atime and mtime by *days_old* days via os.utime."""
    ts = time.time() - days_old * 86_400
    os.utime(path, (ts, ts))


# ---------------------------------------------------------------------------
# (a) _stage_and_delete moves files and returns correct freed bytes
# ---------------------------------------------------------------------------


def test_stage_and_delete_moves_and_removes_files(tmp_path: Path) -> None:
    """Stale files are gone and no staging dir remains; freed bytes are exact."""
    sstate = tmp_path / "sstate"
    sstate.mkdir()
    sizes = [100, 200, 300]
    files = []
    for i, sz in enumerate(sizes):
        p = sstate / f"file_{i}.tar.zst"
        p.write_bytes(b"X" * sz)
        files.append(p)

    removed, freed = _stage_and_delete(files, sstate)

    assert removed == 3
    assert freed == sum(sizes)
    for f in files:
        assert not f.exists(), f"{f} should have been removed"
    gc_dirs = list(sstate.glob(".bakar-gc-*"))
    assert gc_dirs == [], f"staging dirs still present after success: {gc_dirs}"


# ---------------------------------------------------------------------------
# (b) Staging dir is inside the sstate root
# ---------------------------------------------------------------------------


def test_stage_and_delete_staging_dir_is_inside_sstate_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The staging dir is a direct child of effective_dir (same device)."""
    sstate = tmp_path / "sstate"
    sstate.mkdir()
    p = sstate / "entry.tar.zst"
    p.write_bytes(b"data")

    # Intercept os.rename to capture where staging/ was created before rmtree
    import bakar.commands.clean_cache as _cc

    original_rename = _cc.os.rename
    staging_parents: list[Path] = []

    def capturing_rename(src, dst) -> None:
        staging_parents.append(Path(dst).parent)
        original_rename(src, dst)

    monkeypatch.setattr(_cc.os, "rename", capturing_rename)
    _stage_and_delete([p], sstate)

    assert len(staging_parents) == 1, "expected exactly one rename"
    staging_dir = staging_parents[0]
    assert staging_dir.parent == sstate, f"staging dir {staging_dir} is not a direct child of sstate root {sstate}"
    assert staging_dir.name.startswith(".bakar-gc-"), (
        f"staging dir name {staging_dir.name!r} does not match .bakar-gc-* pattern"
    )


# ---------------------------------------------------------------------------
# (c) Collision safety: same basename in different subdirs
# ---------------------------------------------------------------------------


def test_stage_and_delete_collision_safe_same_basename(tmp_path: Path) -> None:
    """Two files with the same basename in different subdirs are both removed."""
    sstate = tmp_path / "sstate"
    sub_a = sstate / "aa"
    sub_b = sstate / "bb"
    sub_a.mkdir(parents=True)
    sub_b.mkdir(parents=True)

    fa = sub_a / "same_name.tar.zst"
    fb = sub_b / "same_name.tar.zst"
    fa.write_bytes(b"A" * 50)
    fb.write_bytes(b"B" * 75)

    removed, freed = _stage_and_delete([fa, fb], sstate)

    assert removed == 2, f"expected 2 files removed, got {removed}"
    assert freed == 125, f"expected 125 bytes freed, got {freed}"
    assert not fa.exists(), f"{fa} should have been removed"
    assert not fb.exists(), f"{fb} should have been removed"


# ---------------------------------------------------------------------------
# (d) clean-cache --dry-run moves/deletes nothing
# ---------------------------------------------------------------------------


def test_dry_run_does_not_move_or_delete_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``--dry-run`` reports candidates but leaves every file untouched."""
    sstate = tmp_path / "sstate-cache"
    sstate.mkdir()
    bucket = sstate / "ab"
    bucket.mkdir()

    stale_files = []
    for i in range(3):
        p = bucket / f"stale_{i}.tar.zst"
        p.write_bytes(b"payload" + str(i).encode())
        _age_file(p, 60.0)
        stale_files.append(p)

    monkeypatch.setenv("SSTATE_DIR", str(sstate))
    monkeypatch.setattr("bakar.commands.clean_cache._atime_tracked", lambda _p: True)

    result = runner.invoke(app, ["clean-cache", "--no-ccache", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "Dry run" in result.output, result.output
    for f in stale_files:
        assert f.exists(), f"{f} was moved or deleted on a --dry-run"
    gc_dirs = list(sstate.glob(".bakar-gc-*"))
    assert gc_dirs == [], f"staging dir created on --dry-run: {gc_dirs}"


# ---------------------------------------------------------------------------
# (e) End-to-end clean-cache --yes --no-ccache reports freed bytes, no staging dir
# ---------------------------------------------------------------------------


def test_clean_cache_yes_reports_freed_bytes_and_no_staging_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--yes --no-ccache`` reports freed bytes and leaves no .bakar-gc-* staging dir."""
    sstate = tmp_path / "sstate-cache"
    sstate.mkdir()
    bucket = sstate / "ab"
    bucket.mkdir()

    stale_files = []
    for i in range(2):
        p = bucket / f"stale_{i}.tar.zst"
        p.write_bytes(b"X" * 1024)
        _age_file(p, 60.0)
        stale_files.append(p)

    fresh = bucket / "fresh.tar.zst"
    fresh.write_bytes(b"F" * 512)

    monkeypatch.setenv("SSTATE_DIR", str(sstate))
    monkeypatch.setattr("bakar.commands.clean_cache._atime_tracked", lambda _p: True)

    result = runner.invoke(app, ["clean-cache", "--no-ccache", "--yes"])

    assert result.exit_code == 0, result.output
    # The output must report deleted count and freed size
    assert "deleted" in result.output, result.output
    assert "deleted 2 files" in result.output, result.output
    # Stale files are gone; fresh file is kept
    for f in stale_files:
        assert not f.exists(), f"{f} should have been deleted"
    assert fresh.exists(), "fresh file should have been kept"
    # No staging dir left behind
    gc_dirs = list(sstate.rglob(".bakar-gc-*"))
    assert gc_dirs == [], f"staging dir still present after clean run: {gc_dirs}"
