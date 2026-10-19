"""Tests for ``bakar.mounts.probe_free_bytes``, the bounded free-space probe."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from bakar import mounts
from bakar.mounts import probe_free_bytes

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


def test_nul_in_a_path_gives_none_for_that_path_and_does_not_raise(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stub_stat(monkeypatch, tmp_path, "echo '10 1024'")
    bad = Path(str(tmp_path / "a") + "\x00b")
    good = tmp_path / "fine"
    assert probe_free_bytes([bad, good], deadline_s=5.0) == {bad: None, good: 10240}


def test_failed_later_spawn_reaps_children_already_started(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[subprocess.Popen[bytes]] = []
    real_popen = subprocess.Popen

    def flaky(argv, **kw):
        if started:
            raise OSError("cannot spawn")
        proc = real_popen(["sleep", "30"], **kw)
        started.append(proc)
        return proc

    monkeypatch.setattr(mounts.subprocess, "Popen", flaky)
    specs = {Path("/a"): ["x"], Path("/b"): ["y"]}
    with pytest.raises(OSError, match="cannot spawn"):
        mounts._run_bounded(specs, time.monotonic() + 5.0)
    (first,) = started
    assert first.poll() is not None
    assert first.stdout is not None
    assert first.stdout.closed
    assert first.stderr is not None
    assert first.stderr.closed
