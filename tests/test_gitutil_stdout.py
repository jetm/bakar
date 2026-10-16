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
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    with open(os.path.join(os.fsencode(tmp_path), b"name-\xff.txt"), "wb"):
        pass
    out = git_stdout(tmp_path, "status", "--porcelain")
    assert out is not None
    assert "name-" in out
    assert out.strip(), "a dirty tree must still read as dirty"


def test_git_stdout_replaces_undecodable_bytes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: dict[str, object] = {}

    def fake_run(cmd, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, stdout="ok�", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert git_stdout(tmp_path, "status") == "ok�"
    assert seen["errors"] == "replace"
