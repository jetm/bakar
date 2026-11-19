"""Tests for ``bakar.mounts.list_regular_files``: bounded listing of regular files."""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from bakar.mounts import _run_bounded, list_regular_files

pytestmark = pytest.mark.unit


def _stub_find(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script_body: str) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "find"
    stub.write_text(f"#!/bin/sh\n{script_body}\n")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")


def _scratch_tempdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    return scratch


def _sleepers(marker: str) -> bool:
    return subprocess.run(["pgrep", "-f", f"sleep {marker}"], check=False, capture_output=True).returncode == 0


def test_lists_regular_files_only(tmp_path: Path) -> None:
    d = tmp_path / "d"
    d.mkdir()
    (d / "a.tar.gz").write_text("x")
    (d / "b").write_text("y")
    (d / "subdir").mkdir()
    (d / "subdir" / "nested").write_text("z")
    (d / "link").symlink_to(d / "a.tar.gz")
    (d / "dirlink").symlink_to(d / "subdir")

    result = list_regular_files([d], deadline_s=5.0)

    assert result[d] is not None
    assert sorted(result[d] or []) == ["a.tar.gz", "b"]


def test_name_with_newline(tmp_path: Path) -> None:
    d = tmp_path / "d"
    d.mkdir()
    (d / "two\nlines").write_text("x")

    assert list_regular_files([d], deadline_s=5.0)[d] == ["two\nlines"]


def test_empty_directory_gives_empty_list(tmp_path: Path) -> None:
    d = tmp_path / "d"
    d.mkdir()

    assert list_regular_files([d], deadline_s=5.0)[d] == []


def test_large_listing_completes(tmp_path: Path) -> None:
    d = tmp_path / "d"
    d.mkdir()
    names = {f"{i:04d}_" + "n" * 50 for i in range(3600)}  # about 200 KB of names
    for name in names:
        (d / name).touch()
    assert sum(len(n) + 1 for n in names) > 200_000

    start = time.monotonic()
    result = list_regular_files([d], deadline_s=20.0)

    assert time.monotonic() - start < 20.0
    assert set(result[d] or []) == names


def test_large_output_through_runner_pipe_is_killed(tmp_path: Path) -> None:
    """Without a file redirect the output outgrows any pipe: the child blocks and is killed.

    4 MiB exceeds the 1 MiB ceiling an unprivileged process can give a pipe, so the result
    does not depend on the kernel's default pipe capacity (a CI runner buffered 200 KB).
    """
    argv = ["sh", "-c", "head -c 4194304 /dev/zero"]
    path = tmp_path / "p"

    start = time.monotonic()
    batch = _run_bounded({path: argv}, time.monotonic() + 0.5)

    assert batch[path] == (None, "", "")
    assert time.monotonic() - start < 3.0


def test_wedged_child_gives_none_and_leaves_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scratch = _scratch_tempdir(tmp_path, monkeypatch)
    _stub_find(tmp_path, monkeypatch, "exec sleep 61.1")
    d = tmp_path / "d"
    d.mkdir()

    start = time.monotonic()
    result = list_regular_files([d], deadline_s=0.3)
    elapsed = time.monotonic() - start

    assert result[d] is None
    assert elapsed < 2.0
    assert list(scratch.iterdir()) == []
    assert not _sleepers("61.1")


def test_two_wedged_directories_share_one_deadline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_find(tmp_path, monkeypatch, "exec sleep 61.2")
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()

    start = time.monotonic()
    result = list_regular_files([a, b], deadline_s=0.4)
    elapsed = time.monotonic() - start

    assert result == {a: None, b: None}
    assert elapsed < 1.0
    assert not _sleepers("61.2")


def test_missing_directory_gives_none(tmp_path: Path) -> None:
    missing = tmp_path / "nope"

    assert list_regular_files([missing], deadline_s=5.0) == {missing: None}


def test_non_utf8_name_is_surrogate_escaped(tmp_path: Path) -> None:
    d = tmp_path / "d"
    d.mkdir()
    raw = os.path.join(os.fsencode(d), b"bad\xff")
    with open(raw, "wb"):
        pass

    result = list_regular_files([d], deadline_s=5.0)

    assert result[d] == ["bad\udcff"]


def test_nul_path_gives_none_for_that_path_only(tmp_path: Path) -> None:
    d = tmp_path / "d"
    d.mkdir()
    (d / "f").touch()
    bad = Path("a\x00b")

    result = list_regular_files([bad, d], deadline_s=5.0)

    assert result[bad] is None
    assert result[d] == ["f"]


def test_spawn_failure_gives_none_for_all(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    d = tmp_path / "d"
    d.mkdir()

    assert list_regular_files([d], deadline_s=1.0) == {d: None}


def test_symlinked_mirror_directory_is_listed(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (real / "a.tar.gz").write_text("x")
    link = tmp_path / "link"
    link.symlink_to(real)

    result = list_regular_files([link], deadline_s=5.0)

    assert result[link] == ["a.tar.gz"]
