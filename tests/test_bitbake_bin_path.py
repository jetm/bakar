"""Tests for the host-mode bitbake launch-PATH composition.

Host builds launch bitbake via kas's ``find_program(ctx.environ['PATH'],
'bitbake')`` and OE's HOSTTOOLS resolves each tool against ``BB_ORIGENV``'s
PATH (the bitbake-launch environment). Two consequences under test:

* :attr:`bakar.config.BuildConfig.bitbake_bin_path` derives the bundled
  bitbake ``bin`` directory per BSP family - it MUST be on the launch PATH or
  the kas->bitbake launch fails.
* :func:`bakar.steps.kas_build._apply_host_mode_env` prepends that directory
  ahead of the buildtools toolbin and the inherited PATH so the pinned
  buildtools gcc wins over the rolling ``/usr/bin/gcc`` while the launch can
  still find bitbake. By default it does not prepend bakar's venv: BB_PYTHON3
  is the SDK python (set by _provision_buildtools), which ships bitbake's deps.

Container mode early-returns: no bitbake_bin injection there.
"""

from __future__ import annotations

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


def _make_cfg(
    workspace: Path,
    *,
    bsp_family: str = "nxp",
    host_mode: bool = False,
    kas_yaml_override: Path | None = None,
) -> BuildConfig:
    return make_build_config(
        workspace=workspace,
        bsp_family=bsp_family,
        repo_branch="scarthgap",
        kas_container_image="jetm/kas-build-env:latest",
        host_mode=host_mode,
        kas_yaml_override=kas_yaml_override,
    )


def _meta_avocado_cfg(workspace: Path, *, host_mode: bool = False) -> BuildConfig:
    """A generic cfg whose YAML lives inside a meta-avocado tree."""
    kas_dir = workspace / "sources" / "meta-avocado" / "kas" / "machine"
    kas_dir.mkdir(parents=True, exist_ok=True)
    kas_yaml = kas_dir / "qemux86-64.yml"
    kas_yaml.write_text("header:\n  version: 16\n", encoding="utf-8")
    return _make_cfg(
        workspace,
        bsp_family="generic",
        host_mode=host_mode,
        kas_yaml_override=kas_yaml,
    )


# ---------------------------------------------------------------------------
# bitbake_bin_path - per-family derivation
# ---------------------------------------------------------------------------


def test_bitbake_bin_path_meta_avocado_is_workspace_bitbake(tmp_path: Path) -> None:
    cfg = _meta_avocado_cfg(tmp_path)
    assert cfg.bitbake_bin_path == tmp_path / "bitbake" / "bin"


def test_bitbake_bin_path_nxp_is_poky_bitbake_bin(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path, bsp_family="nxp")
    assert cfg.bitbake_bin_path == cfg.bsp_root / "sources" / "poky" / "bitbake" / "bin"


def test_bitbake_bin_path_ti_is_sources_bitbake_bin(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path, bsp_family="ti")
    assert cfg.bitbake_bin_path == cfg.bsp_root / "sources" / "bitbake" / "bin"


# ---------------------------------------------------------------------------
# _apply_host_mode_env - launch PATH composition
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_buildtools_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OECORE_NATIVE_SYSROOT", raising=False)
    monkeypatch.delenv(diagnostics.BUILDTOOLS_DIR_ENV, raising=False)
    monkeypatch.setattr(diagnostics, "load_user_config", UserConfig)


def _install_fake_toolchain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Stage a buildtools env-setup script; return its toolchain bin dir."""
    sysroot = tmp_path / "sdk" / "sysroots" / "x86_64"
    toolbin = sysroot / "usr" / "bin"
    toolbin.mkdir(parents=True)
    (toolbin / "gcc").write_text("#!/bin/sh\n")
    script = tmp_path / "environment-setup-x86_64-pokysdk-linux"
    script.write_text("export OECORE_NATIVE_SYSROOT=" + str(sysroot) + "\nexport PATH=" + str(toolbin) + ":$PATH\n")
    monkeypatch.setenv(diagnostics.BUILDTOOLS_DIR_ENV, str(tmp_path))
    return toolbin


def test_host_mode_path_orders_bitbake_then_buildtools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Default host build: bitbake bin first, then the buildtools toolbin ahead of
    /usr/bin, and BB_PYTHON3 is the SDK python (bakar's venv is not prepended)."""
    toolbin = _install_fake_toolchain(tmp_path, monkeypatch)
    cfg = _meta_avocado_cfg(tmp_path, host_mode=True)
    cfg.bitbake_bin_path.mkdir(parents=True, exist_ok=True)

    passthrough: dict[str, str] = {"PATH": "/usr/bin:/bin"}
    kas_build._apply_host_mode_env(cfg, None, passthrough)

    parts = passthrough["PATH"].split(":")
    bb_bin = str(cfg.bitbake_bin_path)
    # No bakar venv prepend in the default case: bitbake bin leads, the SDK
    # toolbin (carrying the SDK python and gcc) sits ahead of /usr/bin.
    assert parts[0] == bb_bin, parts
    assert parts.index(bb_bin) < parts.index(str(toolbin))
    assert parts.index(str(toolbin)) < parts.index("/usr/bin")
    assert passthrough["BB_PYTHON3"] == str(toolbin / "python3")


def test_host_mode_missing_bitbake_bin_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A wrong derivation (no bin dir on disk) must fail loud, not produce a broken launch."""
    _install_fake_toolchain(tmp_path, monkeypatch)
    cfg = _meta_avocado_cfg(tmp_path, host_mode=True)
    # Deliberately do NOT create cfg.bitbake_bin_path.

    passthrough: dict[str, str] = {"PATH": "/usr/bin:/bin"}
    with pytest.raises(kas_build.BitbakeBinMissingError):
        kas_build._apply_host_mode_env(cfg, None, passthrough)


def test_container_mode_leaves_path_unchanged(tmp_path: Path) -> None:
    """Container mode early-returns: no bitbake_bin injection, PATH untouched."""
    cfg = _make_cfg(tmp_path, host_mode=False)
    passthrough: dict[str, str] = {"PATH": "/usr/bin:/bin"}
    kas_build._apply_host_mode_env(cfg, None, passthrough)
    assert passthrough["PATH"] == "/usr/bin:/bin"


def test_host_mode_no_existence_check_when_provision_disabled(tmp_path: Path) -> None:
    """Script-gen/dry-run rendering (provision_buildtools=False) must not raise on a
    missing bitbake bin or a missing toolchain."""
    cfg = _meta_avocado_cfg(tmp_path, host_mode=True)
    # No bitbake_bin_path on disk, no toolchain installed.
    passthrough: dict[str, str] = {"PATH": "/usr/bin:/bin"}
    kas_build._apply_host_mode_env(cfg, None, passthrough, provision_buildtools=False)
    # bitbake bin is still prepended (only the existence check is gated); no raise.
    parts = passthrough["PATH"].split(":")
    assert str(cfg.bitbake_bin_path) in parts
    assert passthrough["PATH"].endswith("/usr/bin:/bin")
