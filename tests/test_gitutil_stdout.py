"""Tests for ``bakar.gitutil.git_stdout`` decoding behaviour."""

from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING

import pytest

from bakar.gitutil import git_stdout

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


def test_git_stdout_survives_non_utf8_filename(tmp_path: Path) -> None:
    """Raw non-UTF-8 filename bytes reach the decoder and are replaced, not raised."""
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    with open(os.path.join(os.fsencode(tmp_path), b"name-\xff.txt"), "wb"):
        pass
    # quotePath=false makes git print the name's raw bytes; the default
    # octal-quotes them into ASCII, which would never exercise the decode.
    raw = subprocess.run(
        ["git", "-C", str(tmp_path), "-c", "core.quotePath=false", "status", "--porcelain"],
        capture_output=True,
        check=True,
    ).stdout
    assert b"\xff" in raw
    out = git_stdout(tmp_path, "-c", "core.quotePath=false", "status", "--porcelain")
    assert out is not None
    assert "name-�.txt" in out
    assert out.strip(), "a dirty tree must still read as dirty"
