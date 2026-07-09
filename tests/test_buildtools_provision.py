"""Tests for buildtools-extended provisioning on the host build path.

Host builds must run against the pinned ``buildtools-extended`` toolchain, not
the rolling Arch system gcc. The contract under test:

* :func:`bakar.diagnostics.detect_buildtools` locates the toolchain via an
  already-sourced ``OECORE_NATIVE_SYSROOT`` or via ``BAKAR_BUILDTOOLS_DIR``.
* :func:`bakar.steps.kas_build._provision_buildtools` raises
  :class:`~bakar.steps.kas_build.BuildtoolsMissingError` (naming the toolchain)
  when it is absent in host mode, and injects the sourced env when present.

The headline falsifier: a host build with the pinned toolchain absent must fail
loudly naming it and must never silently fall back to ``/usr/bin/gcc``.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import pytest

from bakar import diagnostics
from bakar.steps import kas_build
from bakar.user_config import UserConfig
from tests.conftest import make_build_config

if TYPE_CHECKING:
    from pathlib import Path

    from bakar.config import BuildConfig

pytestmark = pytest.mark.unit


def _make_cfg(workspace: Path, *, host_mode: bool = False) -> BuildConfig:
    """Minimal BuildConfig mirroring tests.test_run_build_host._make_cfg."""
    return make_build_config(workspace=workspace, host_mode=host_mode)


@pytest.fixture(autouse=True)
def _clear_buildtools_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start each test from a clean slate (no toolchain in the ambient env).

    Also neutralizes the ``[build] buildtools_dir`` config fallback so a real
    ``~/.config/bakar/config.toml`` on the dev's host cannot leak in; tests that
    exercise the config path patch ``load_user_config`` themselves.
    """
    monkeypatch.delenv("OECORE_NATIVE_SYSROOT", raising=False)
    monkeypatch.delenv(diagnostics.BUILDTOOLS_DIR_ENV, raising=False)
    monkeypatch.setattr(diagnostics, "load_user_config", UserConfig)


# ---------------------------------------------------------------------------
# detect_buildtools
# ---------------------------------------------------------------------------


def test_detect_absent_when_nothing_set() -> None:
    tc = diagnostics.detect_buildtools()
    assert tc.present is False
    assert diagnostics.BUILDTOOLS_DIR_ENV in tc.detail


def test_detect_already_sourced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sysroot = tmp_path / "sysroot"
    gcc = sysroot / "usr" / "bin" / "gcc"
    gcc.parent.mkdir(parents=True)
    gcc.write_text("#!/bin/sh\n")
    monkeypatch.setenv("OECORE_NATIVE_SYSROOT", str(sysroot))

    tc = diagnostics.detect_buildtools()

    assert tc.present is True
    assert tc.sysroot == sysroot
    assert tc.env_script is None


def test_detect_sourced_var_without_gcc_is_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # OECORE_NATIVE_SYSROOT set but the gcc is missing -> not a usable toolchain.
    monkeypatch.setenv("OECORE_NATIVE_SYSROOT", str(tmp_path / "nope"))
    tc = diagnostics.detect_buildtools()
    assert tc.present is False


def test_detect_via_install_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    script = tmp_path / "environment-setup-x86_64-pokysdk-linux"
    script.write_text("export OECORE_NATIVE_SYSROOT=/x\n")
    monkeypatch.setenv(diagnostics.BUILDTOOLS_DIR_ENV, str(tmp_path))

    tc = diagnostics.detect_buildtools()

    assert tc.present is True
    assert tc.env_script == script


def test_detect_install_dir_without_script_is_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(diagnostics.BUILDTOOLS_DIR_ENV, str(tmp_path))
    tc = diagnostics.detect_buildtools()
    assert tc.present is False
    assert "no environment-setup-*" in tc.detail


def _toolchain_dir(parent: Path) -> Path:
    """Create a dir holding a buildtools-extended env-setup script."""
    (parent / "environment-setup-x86_64-pokysdk-linux").write_text("export OECORE_NATIVE_SYSROOT=/x\n")
    return parent


def test_detect_via_config_when_env_unset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Env var unset, [build] buildtools_dir set -> present at the configured dir."""
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    _toolchain_dir(cfg_dir)
    monkeypatch.setattr(diagnostics, "load_user_config", lambda: UserConfig(buildtools_dir=str(cfg_dir)))

    tc = diagnostics.detect_buildtools()

    assert tc.present is True
    assert tc.env_script == cfg_dir / "environment-setup-x86_64-pokysdk-linux"


def test_detect_env_wins_over_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Both env and config name valid toolchains -> the env-var dir is resolved."""
    env_dir = tmp_path / "env"
    env_dir.mkdir()
    _toolchain_dir(env_dir)
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    _toolchain_dir(cfg_dir)
    monkeypatch.setenv(diagnostics.BUILDTOOLS_DIR_ENV, str(env_dir))
    monkeypatch.setattr(diagnostics, "load_user_config", lambda: UserConfig(buildtools_dir=str(cfg_dir)))

    tc = diagnostics.detect_buildtools()

    assert tc.present is True
    assert tc.env_script is not None
    assert tc.env_script.parent == env_dir
    assert tc.env_script.parent != cfg_dir


def test_detect_absent_when_neither_env_nor_config_set() -> None:
    """Neither env var nor config field set -> present=False (loud-failure contract)."""
    # The autouse fixture already patches load_user_config to an empty config.
    tc = diagnostics.detect_buildtools()
    assert tc.present is False


# ---------------------------------------------------------------------------
# _provision_buildtools - the loud-failure falsifier
# ---------------------------------------------------------------------------


def test_provision_missing_toolchain_fails_loudly(tmp_path: Path) -> None:
    """Host build + absent toolchain -> raise naming it, never fall back to system gcc."""
    cfg = _make_cfg(tmp_path, host_mode=True)
    passthrough: dict[str, str] = {"PATH": "/usr/bin:/bin"}

    with pytest.raises(kas_build.BuildtoolsMissingError) as exc:
        kas_build._provision_buildtools(cfg, passthrough)

    msg = str(exc.value)
    assert "buildtools-extended" in msg
    # The diagnostic must refuse the system-gcc fallback explicitly.
    assert "/usr/bin/gcc" in msg
    # And it must NOT have silently mutated PATH to keep the system gcc usable
    # as if provisioning had succeeded.
    assert passthrough["PATH"] == "/usr/bin:/bin"


def test_provision_noop_in_container_mode(tmp_path: Path) -> None:
    """Container builds get their toolchain from the kas image - never raise."""
    cfg = _make_cfg(tmp_path, host_mode=False)
    passthrough: dict[str, str] = {"PATH": "/usr/bin:/bin"}
    kas_build._provision_buildtools(cfg, passthrough)
    assert passthrough["PATH"] == "/usr/bin:/bin"


def test_provision_already_sourced_does_not_raise(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sysroot = tmp_path / "sysroot"
    gcc = sysroot / "usr" / "bin" / "gcc"
    gcc.parent.mkdir(parents=True)
    gcc.write_text("#!/bin/sh\n")
    monkeypatch.setenv("OECORE_NATIVE_SYSROOT", str(sysroot))

    cfg = _make_cfg(tmp_path, host_mode=True)
    passthrough: dict[str, str] = {"PATH": "/usr/bin:/bin"}
    # Already sourced -> no env script to re-source, no raise.
    kas_build._provision_buildtools(cfg, passthrough)


def test_provision_sources_env_script(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When found via install dir, the script's PATH and OE vars land in the env."""
    sysroot = tmp_path / "sdk" / "sysroots" / "x86_64"
    toolbin = sysroot / "usr" / "bin"
    toolbin.mkdir(parents=True)
    (toolbin / "gcc").write_text("#!/bin/sh\n")

    script = tmp_path / "environment-setup-x86_64-pokysdk-linux"
    script.write_text(
        "export OECORE_NATIVE_SYSROOT=" + str(sysroot) + "\n"
        "export PATH=" + str(toolbin) + ":$PATH\n"
        "export CC='gcc -pinned'\n"
    )
    monkeypatch.setenv(diagnostics.BUILDTOOLS_DIR_ENV, str(tmp_path))

    cfg = _make_cfg(tmp_path, host_mode=True)
    passthrough: dict[str, str] = {"PATH": "/usr/bin:/bin"}
    kas_build._provision_buildtools(cfg, passthrough)

    # The pinned toolchain bin must be on PATH ahead of the system dirs.
    assert str(toolbin) in passthrough["PATH"]
    assert passthrough["PATH"].index(str(toolbin)) < passthrough["PATH"].index("/usr/bin")
    assert passthrough["OECORE_NATIVE_SYSROOT"] == str(sysroot)
    assert passthrough["CC"] == "gcc -pinned"


def test_apply_host_mode_env_provisions_before_python(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_apply_host_mode_env runs provisioning; absent toolchain -> loud failure."""
    cfg = _make_cfg(tmp_path, host_mode=True)
    passthrough: dict[str, str] = {"PATH": "/usr/bin:/bin"}
    with pytest.raises(kas_build.BuildtoolsMissingError):
        kas_build._apply_host_mode_env(cfg, None, passthrough)


# ---------------------------------------------------------------------------
# SDK python selection - host bitbake must run under the buildtools-extended
# python (it ships bitbake's runtime deps, e.g. websockets for the hashserv ws
# client), not bakar's venv python.
# ---------------------------------------------------------------------------


def _sdk_env_script(tmp_path: Path) -> Path:
    """Create a fake buildtools SDK (gcc + python3) and its env-setup script.

    Returns the sysroot. The script prepends the SDK bin to PATH but does NOT
    export OECORE_NATIVE_SYSROOT, matching the real buildtools-extended file, so
    BB_PYTHON3 derivation must read the SDK bin from PATH.
    """
    sysroot = tmp_path / "sdk" / "sysroots" / "x86_64"
    toolbin = sysroot / "usr" / "bin"
    toolbin.mkdir(parents=True)
    (toolbin / "gcc").write_text("#!/bin/sh\n")
    (toolbin / "python3").write_text("#!/bin/sh\n")
    script = tmp_path / "environment-setup-x86_64-pokysdk-linux"
    script.write_text("export PATH=" + str(toolbin) + ":$PATH\n")
    return sysroot


def test_provision_sets_bb_python3_to_sdk_python(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """env-script path: BB_PYTHON3 points at the SDK's python3 under the sysroot."""
    sysroot = _sdk_env_script(tmp_path)
    monkeypatch.setenv(diagnostics.BUILDTOOLS_DIR_ENV, str(tmp_path))
    cfg = _make_cfg(tmp_path, host_mode=True)
    passthrough: dict[str, str] = {"PATH": "/usr/bin:/bin"}
    kas_build._provision_buildtools(cfg, passthrough)
    assert passthrough["BB_PYTHON3"] == str(sysroot / "usr" / "bin" / "python3")


def test_provision_sets_bb_python3_already_sourced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """already-sourced path: BB_PYTHON3 derived from OECORE_NATIVE_SYSROOT."""
    sysroot = tmp_path / "sysroot"
    gcc = sysroot / "usr" / "bin" / "gcc"
    gcc.parent.mkdir(parents=True)
    gcc.write_text("#!/bin/sh\n")
    monkeypatch.setenv("OECORE_NATIVE_SYSROOT", str(sysroot))
    cfg = _make_cfg(tmp_path, host_mode=True)
    passthrough: dict[str, str] = {"PATH": "/usr/bin:/bin"}
    kas_build._provision_buildtools(cfg, passthrough)
    assert passthrough["BB_PYTHON3"] == str(sysroot / "usr" / "bin" / "python3")


def test_apply_host_mode_uses_sdk_python_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Default host build runs under the SDK python, not bakar's interpreter."""
    sysroot = _sdk_env_script(tmp_path)
    monkeypatch.setenv(diagnostics.BUILDTOOLS_DIR_ENV, str(tmp_path))
    cfg = _make_cfg(tmp_path, host_mode=True)
    cfg.bitbake_bin_path.mkdir(parents=True)
    passthrough: dict[str, str] = {"PATH": "/usr/bin:/bin"}
    kas_build._apply_host_mode_env(cfg, None, passthrough)
    assert passthrough["BB_PYTHON3"] == str(sysroot / "usr" / "bin" / "python3")
    assert passthrough["BB_PYTHON3"] != sys.executable


def test_apply_host_mode_explicit_python_overrides_sdk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit python_executable (stress-parse) still wins over the SDK python."""
    _sdk_env_script(tmp_path)
    monkeypatch.setenv(diagnostics.BUILDTOOLS_DIR_ENV, str(tmp_path))
    cfg = _make_cfg(tmp_path, host_mode=True)
    cfg.bitbake_bin_path.mkdir(parents=True)
    custom = tmp_path / "custom" / "python3"
    custom.parent.mkdir(parents=True)
    custom.write_text("#!/bin/sh\n")
    passthrough: dict[str, str] = {"PATH": "/usr/bin:/bin"}
    kas_build._apply_host_mode_env(cfg, custom, passthrough)
    assert passthrough["BB_PYTHON3"] == str(custom.resolve())


def test_apply_host_mode_dry_run_falls_back_to_sys_executable(tmp_path: Path) -> None:
    """Dry-run/script-gen (no provisioning) falls back to bakar's interpreter."""
    cfg = _make_cfg(tmp_path, host_mode=True)
    passthrough: dict[str, str] = {"PATH": "/usr/bin:/bin"}
    kas_build._apply_host_mode_env(cfg, None, passthrough, provision_buildtools=False)
    assert passthrough["BB_PYTHON3"] == sys.executable


# ---------------------------------------------------------------------------
# Release-scoped detection - a buildtools-extended install built for one
# Yocto release (e.g. scarthgap) must never silently satisfy a build against
# a different release (e.g. wrynose): the two can require different host
# gcc/glibc/python baselines.
# ---------------------------------------------------------------------------


def test_detect_via_release_scoped_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """release_key given + a matching [build.buildtools_dirs] entry -> present at that dir."""
    release_dir = tmp_path / "wrynose-bt"
    release_dir.mkdir()
    _toolchain_dir(release_dir)
    monkeypatch.setattr(
        diagnostics,
        "load_user_config",
        lambda: UserConfig(buildtools_dirs={"wrynose-abc123": str(release_dir)}),
    )

    tc = diagnostics.detect_buildtools(release_key="wrynose-abc123")

    assert tc.present is True
    assert tc.env_script == release_dir / "environment-setup-x86_64-pokysdk-linux"


def test_detect_release_scoped_does_not_fall_back_to_flat_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A release_key with no matching entry must NOT silently reuse the untagged
    [build] buildtools_dir - that would defeat the point of tagging (a
    scarthgap-built toolchain must never satisfy a wrynose build)."""
    flat_dir = tmp_path / "scarthgap-bt"
    flat_dir.mkdir()
    _toolchain_dir(flat_dir)
    monkeypatch.setattr(
        diagnostics,
        "load_user_config",
        lambda: UserConfig(buildtools_dir=str(flat_dir), buildtools_dirs={}),
    )

    tc = diagnostics.detect_buildtools(release_key="wrynose-abc123")

    assert tc.present is False


def test_detect_release_scoped_env_var_still_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """BAKAR_BUILDTOOLS_DIR stays the explicit escape hatch even with release_key set."""
    env_dir = tmp_path / "env"
    env_dir.mkdir()
    _toolchain_dir(env_dir)
    monkeypatch.setenv(diagnostics.BUILDTOOLS_DIR_ENV, str(env_dir))

    tc = diagnostics.detect_buildtools(release_key="wrynose-abc123")

    assert tc.present is True
    assert tc.env_script is not None
    assert tc.env_script.parent == env_dir


def test_resolve_oe_core_release_key_reads_oe_core_git_commit(tmp_path: Path) -> None:
    """The release key is the short commit hash of workspace/openembedded-core."""
    import subprocess

    oe_core = tmp_path / "openembedded-core"
    oe_core.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=oe_core, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=oe_core, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=oe_core, check=True)
    (oe_core / "README").write_text("x")
    subprocess.run(["git", "add", "README"], cwd=oe_core, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=oe_core, check=True)
    expected = subprocess.run(
        ["git", "-C", str(oe_core), "rev-parse", "--short=12", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    key = diagnostics.resolve_oe_core_release_key(tmp_path)

    assert key == expected


def test_resolve_oe_core_release_key_none_when_no_oe_core_checkout(tmp_path: Path) -> None:
    """No openembedded-core dir under the workspace -> None, not a raise."""
    assert diagnostics.resolve_oe_core_release_key(tmp_path) is None
