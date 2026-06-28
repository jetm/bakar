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

from typing import TYPE_CHECKING

import pytest

from bakar import diagnostics
from bakar.config import BuildConfig
from bakar.steps import kas_build

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


def _make_cfg(workspace: Path, *, host_mode: bool = False) -> BuildConfig:
    """Minimal BuildConfig mirroring tests.test_run_build_host._make_cfg."""
    return BuildConfig(
        workspace=workspace,
        bsp_family="nxp",  # type: ignore[arg-type]
        machine="imx8mp-var-dart",
        distro="fsl-imx-xwayland",
        image="core-image-minimal",
        manifest="imx-6.6.52-2.2.2.xml",
        repo_url="https://example.invalid/repo.git",
        repo_branch="imx-6.6.52-2.2.2",
        kas_container_image="jetm/kas-build-env:5.2-f40",
        host_mode=host_mode,
    )


@pytest.fixture(autouse=True)
def _clear_buildtools_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start each test from a clean slate (no toolchain in the ambient env)."""
    monkeypatch.delenv("OECORE_NATIVE_SYSROOT", raising=False)
    monkeypatch.delenv(diagnostics.BUILDTOOLS_DIR_ENV, raising=False)


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
