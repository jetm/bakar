"""Tests for ``bakar.mounts.probe_free_bytes``, the bounded free-space probe."""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING

import pytest

from bakar.mounts import probe_free_bytes

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


def _stub_stat(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, script_body: str) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "stat"
    stub.write_text(f"#!/bin/sh\n{script_body}\n")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")


def test_stub_output_is_multiplied(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _stub_stat(monkeypatch, tmp_path, "echo '100 4096'")
    target = tmp_path / "x"
    assert probe_free_bytes([target], deadline_s=5.0) == {target: 409600}


def test_enoent_gives_none(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _stub_stat(monkeypatch, tmp_path, 'echo "No such file or directory" >&2\nexit 1')
    target = tmp_path / "x"
    assert probe_free_bytes([target], deadline_s=5.0) == {target: None}


@pytest.mark.parametrize("output", ["garbage", "1 2 3", "-1 4096", "12", ""])
def test_garbage_output_gives_none(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, output: str) -> None:
    _stub_stat(monkeypatch, tmp_path, f"echo '{output}'")
    target = tmp_path / "x"
    assert probe_free_bytes([target], deadline_s=5.0) == {target: None}


def test_unresponsive_path_reports_none_within_the_deadline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _stub_stat(monkeypatch, tmp_path, "sleep 30")
    target = tmp_path / "x"
    start = time.monotonic()
    result = probe_free_bytes([target], deadline_s=1.0)
    elapsed = time.monotonic() - start
    assert result == {target: None}
    assert elapsed < 2.5


def test_wedged_path_does_not_poison_healthy_one(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _stub_stat(
        monkeypatch,
        tmp_path,
        'case "$*" in */sick) sleep 30;; *) echo "10 1024";; esac',
    )
    wedged = tmp_path / "sick"
    healthy = tmp_path / "fine"
    start = time.monotonic()
    result = probe_free_bytes([wedged, healthy], deadline_s=1.0)
    assert time.monotonic() - start < 2.5
    assert result == {wedged: None, healthy: 10240}
