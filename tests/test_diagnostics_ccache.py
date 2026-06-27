"""Unit tests for ``bakar.diagnostics.probe_ccache``.

``subprocess.run`` and ``shutil.which`` are patched so no real ``ccache``
binary runs. The cache dir is a real tmp_path so the existence guard behaves.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from bakar.diagnostics import probe_ccache

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit

_STATS = "cache_hit_direct 8\ncache_hit_preprocessed 2\ncache_miss 4\n"


def _mock_run(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


def test_probe_ccache_parses_hits_and_misses(tmp_path: Path) -> None:
    ccache_dir = tmp_path / "ccache"
    ccache_dir.mkdir()
    with (
        patch("bakar.diagnostics.shutil.which", return_value="/usr/bin/ccache"),
        patch("bakar.diagnostics.subprocess.run", return_value=_mock_run(_STATS)),
    ):
        report = probe_ccache(ccache_dir)
    assert report.available is True
    assert report.cache_hits == 10
    assert report.cache_misses == 4
    assert report.hit_rate == pytest.approx(100.0 * 10 / 14)


def test_probe_ccache_absent_dir_is_unavailable(tmp_path: Path) -> None:
    report = probe_ccache(tmp_path / "does-not-exist")
    assert report.available is False
    assert report.error is not None
    assert report.cache_hits == 0
    assert report.cache_misses == 0


def test_probe_ccache_missing_binary_is_unavailable(tmp_path: Path) -> None:
    ccache_dir = tmp_path / "ccache"
    ccache_dir.mkdir()
    with patch("bakar.diagnostics.shutil.which", return_value=None):
        report = probe_ccache(ccache_dir)
    assert report.available is False
    assert report.error == "ccache binary not on PATH"


def test_probe_ccache_nonzero_exit_is_unavailable(tmp_path: Path) -> None:
    ccache_dir = tmp_path / "ccache"
    ccache_dir.mkdir()
    with (
        patch("bakar.diagnostics.shutil.which", return_value="/usr/bin/ccache"),
        patch("bakar.diagnostics.subprocess.run", return_value=_mock_run("", returncode=1)),
    ):
        report = probe_ccache(ccache_dir)
    assert report.available is False
