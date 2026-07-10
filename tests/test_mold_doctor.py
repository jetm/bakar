"""Tests for the mold C++20 build-compiler doctor gate.

Covers the three task-6.2 behaviors of :func:`bakar.diagnostics.check_mold_compiler`:
a BLOCKing failure when the mode-appropriate build compiler cannot compile a
C++20 probe, a PASS when it can, and no mold finding when ``cfg.mold`` is False.
The probe itself runs against fake compiler scripts so the tests are
deterministic and need no real buildtools toolchain (A11).
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from bakar import diagnostics
from bakar.config import BuildConfig
from bakar.diagnostics import SHARED_CHECKS, Severity, Status, check_mold_compiler


def _mold_cfg(*, mold: bool = True, host_mode: bool = True) -> BuildConfig:
    """Return a minimal BuildConfig for the mold doctor gate tests."""
    return BuildConfig(
        workspace=Path("/tmp"),
        bsp_family="nxp",  # type: ignore[arg-type]
        machine="m",
        distro="d",
        image="i",
        manifest="x.xml",
        repo_url="https://example.com",
        repo_branch="main",
        kas_container_image="img:latest",
        mold=mold,
        host_mode=host_mode,
    )


def _fake_compiler(tmp_path: Path, *, exit_code: int, name: str = "g++") -> Path:
    """Write an executable stub that ignores its args and exits ``exit_code``."""
    script = tmp_path / name
    script.write_text(f"#!/bin/sh\nexit {exit_code}\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return script


@pytest.mark.unit
def test_mold_compiler_in_shared_checks() -> None:
    """The check is wired into the shared list so ``run_all`` runs it."""
    assert check_mold_compiler in SHARED_CHECKS


@pytest.mark.unit
def test_mold_compiler_no_finding_when_mold_off() -> None:
    """No mold finding (skip) when ``cfg.mold`` is False - the probe never runs."""
    result = check_mold_compiler(_mold_cfg(mold=False))

    assert result.status is Status.SKIP
    assert result.severity is Severity.INFO


@pytest.mark.unit
def test_mold_compiler_blocks_on_cxx20_probe_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A compiler that fails the C++20 probe yields a BLOCKing failure.

    Falsifier guard: the gate must BLOCK when the mode compiler lacks C++20,
    turning a deep do_compile failure into a fast pre-flight stop.
    """
    compiler = _fake_compiler(tmp_path, exit_code=1)
    monkeypatch.setattr(diagnostics, "_mold_build_compiler", lambda cfg: compiler)

    result = check_mold_compiler(_mold_cfg())

    assert result.status is Status.FAIL
    assert result.severity is Severity.BLOCK
    assert "C++20" in result.message


@pytest.mark.unit
def test_mold_compiler_passes_on_cxx20_probe_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A compiler that compiles the C++20 probe yields a PASS, no finding."""
    compiler = _fake_compiler(tmp_path, exit_code=0)
    monkeypatch.setattr(diagnostics, "_mold_build_compiler", lambda cfg: compiler)

    result = check_mold_compiler(_mold_cfg())

    assert result.status is Status.PASS
    assert result.severity is Severity.BLOCK


@pytest.mark.unit
def test_mold_compiler_skips_container_mode_when_unprobeable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Container mode with no probeable compiler skips-with-info, never BLOCKs the wrong one."""
    monkeypatch.setattr(diagnostics, "_mold_build_compiler", lambda cfg: None)

    result = check_mold_compiler(_mold_cfg(host_mode=False))

    assert result.status is Status.SKIP
    assert result.severity is Severity.INFO
