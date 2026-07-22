"""Tests for the host-mode preflight diagnostic (``check_host_preflight``).

The doctor host-mode gate must enforce two host-build preconditions before a
build spawns: the pinned ``buildtools-extended`` toolchain is present, and its
``-native`` gcc (carrying uninative's shipped loader) actually runs on the host
kernel.

The headline falsifier: when ``buildtools-extended`` is absent, the check must
report ``FAIL`` (not ``PASS``) so doctor never green-lights a host build that
would silently fall back to the system gcc.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from bakar import diagnostics
from bakar.diagnostics import Severity, Status, check_host_preflight
from bakar.user_config import UserConfig
from tests.conftest import make_build_config

if TYPE_CHECKING:
    from pathlib import Path

    from bakar.config import BuildConfig

pytestmark = pytest.mark.unit


def _make_cfg(workspace: Path, *, host_mode: bool = True) -> BuildConfig:
    return make_build_config(workspace=workspace, host_mode=host_mode)


@pytest.fixture(autouse=True)
def _clear_buildtools_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clean slate: override the conftest autouse toolchain fixture.

    The suite-wide ``_fake_buildtools_toolchain`` fixture sets
    ``OECORE_NATIVE_SYSROOT``. This autouse fixture runs after it and clears the
    detection env vars so each test starts with no toolchain; tests that want a
    toolchain set it explicitly.
    """
    monkeypatch.delenv("OECORE_NATIVE_SYSROOT", raising=False)
    monkeypatch.delenv(diagnostics.BUILDTOOLS_DIR_ENV, raising=False)


def _sourced_toolchain(root: Path, monkeypatch: pytest.MonkeyPatch, *, gcc_body: str) -> Path:
    """Create a sourced buildtools sysroot with an executable gcc stub."""
    sysroot = root / "sysroot"
    gcc = sysroot / "usr" / "bin" / "gcc"
    gcc.parent.mkdir(parents=True)
    gcc.write_text(gcc_body)
    gcc.chmod(0o755)
    monkeypatch.setenv("OECORE_NATIVE_SYSROOT", str(sysroot))
    return gcc


# ---------------------------------------------------------------------------
# Falsifier: absent toolchain must FAIL, never PASS
# ---------------------------------------------------------------------------


def test_fails_when_buildtools_absent(tmp_path: Path) -> None:
    """The headline falsifier: no toolchain -> FAIL (not PASS)."""
    result = check_host_preflight(_make_cfg(tmp_path))

    assert result.status is Status.FAIL
    assert result.severity is Severity.BLOCK
    assert "buildtools-extended" in result.message
    assert result.fix_hint is not None


def test_fails_when_dir_env_has_no_script(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install_dir = tmp_path / "buildtools"
    install_dir.mkdir()
    monkeypatch.setenv(diagnostics.BUILDTOOLS_DIR_ENV, str(install_dir))

    result = check_host_preflight(_make_cfg(tmp_path))

    assert result.status is Status.FAIL
    assert result.severity is Severity.BLOCK


# ---------------------------------------------------------------------------
# Container mode skips the host toolchain probe
# ---------------------------------------------------------------------------


def test_skips_in_container_mode(tmp_path: Path) -> None:
    result = check_host_preflight(_make_cfg(tmp_path, host_mode=False))

    assert result.status is Status.SKIP
    assert result.severity is Severity.INFO


# ---------------------------------------------------------------------------
# Loader probe
# ---------------------------------------------------------------------------


def test_passes_when_toolchain_present_and_loader_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _sourced_toolchain(tmp_path, monkeypatch, gcc_body="#!/bin/sh\necho 'gcc (stub) 13.2.0'\n")

    result = check_host_preflight(_make_cfg(tmp_path))

    assert result.status is Status.PASS
    assert result.severity is Severity.BLOCK
    assert "uninative loader runs" in result.message


def test_fails_when_loader_not_runnable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A present-but-unrunnable gcc (uninative loader broken) -> FAIL."""
    _sourced_toolchain(tmp_path, monkeypatch, gcc_body="#!/bin/sh\nexit 127\n")

    result = check_host_preflight(_make_cfg(tmp_path))

    assert result.status is Status.FAIL
    assert result.severity is Severity.BLOCK
    assert "uninative loader" in result.message


# ---------------------------------------------------------------------------
# Registration in the doctor check list
# ---------------------------------------------------------------------------


def test_registered_in_shared_checks() -> None:
    assert check_host_preflight in diagnostics.SHARED_CHECKS


def test_grouped_in_check_groups() -> None:
    grouped_names = {name for _, names in diagnostics.CHECK_GROUPS for name in names}
    assert "host-preflight" in grouped_names


# ---------------------------------------------------------------------------
# Release-scoped detection - the doctor gate must consult the SAME
# release-scoped [build.buildtools_dirs] entry that `bakar setup` installs
# into, not just the legacy flat [build] buildtools_dir. Without this, a
# release-scoped install (e.g. a wrynose toolchain keyed to its oe-core
# commit) is invisible to `bakar build`'s own pre-flight gate even though
# `bakar setup` already installed it correctly.
# ---------------------------------------------------------------------------


def _oe_core_release(tmp_path: Path, codename: str = "scarthgap") -> str:
    """Give workspace/openembedded-core a layer.conf declaring ``codename``; return it.

    The release key is the Yocto release codename read from
    ``meta/conf/layer.conf`` (LAYERSERIES_CORENAMES), not the oe-core commit.
    """
    conf = tmp_path / "openembedded-core" / "meta" / "conf"
    conf.mkdir(parents=True)
    (conf / "layer.conf").write_text(f'LAYERSERIES_CORENAMES = "{codename}"\n')
    return codename


def test_passes_via_release_scoped_config_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A release-scoped [build.buildtools_dirs] entry (no flat buildtools_dir,
    no env var) must satisfy the gate - this is exactly what `bakar setup`
    persists for a release-scoped install."""
    release_key = _oe_core_release(tmp_path)
    install_dir = tmp_path / "bt"
    install_dir.mkdir()
    (install_dir / "environment-setup-x86_64-pokysdk-linux").write_text("export OECORE_NATIVE_SYSROOT=/x\n")
    monkeypatch.setattr(
        diagnostics,
        "load_user_config",
        lambda: UserConfig(buildtools_dirs={release_key: str(install_dir)}),
    )

    result = check_host_preflight(_make_cfg(tmp_path))

    assert result.status is not Status.FAIL
    assert "not found" not in result.message


def test_fails_when_flat_config_set_but_release_key_entry_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A workspace with a resolvable release key must NOT fall back to the
    flat buildtools_dir - falling back would let a differently-tagged
    toolchain silently satisfy this release's gate."""
    _oe_core_release(tmp_path)
    flat_dir = tmp_path / "flat-bt"
    flat_dir.mkdir()
    (flat_dir / "environment-setup-x86_64-pokysdk-linux").write_text("export OECORE_NATIVE_SYSROOT=/x\n")
    monkeypatch.setattr(
        diagnostics,
        "load_user_config",
        lambda: UserConfig(buildtools_dir=str(flat_dir), buildtools_dirs={}),
    )

    result = check_host_preflight(_make_cfg(tmp_path))

    assert result.status is Status.FAIL
