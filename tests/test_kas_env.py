"""Unit tests for varis_build.steps.kas_build._build_env.

Pins the workspace-root ccache bind-mount that supersedes the dangling
per-BSP ``ccache`` symlinks. Without this mount, kas-container would
have ``/work/ccache`` resolve to a symlink that points one level
above ``/work`` - a path the container cannot reach.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from varis_build.config import BuildConfig
from varis_build.steps.kas_build import _build_env

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _make_cfg(workspace: Path, bsp_family: str = "ti") -> BuildConfig:
    return BuildConfig(
        workspace=workspace,
        bsp_family=bsp_family,  # type: ignore[arg-type]
        machine="am62x-var-som",
        distro="arago",
        image="var-thin-image",
        manifest="processor-sdk-scarthgap-chromium-11.00.09.04-config_var01.txt",
        repo_url="https://example.invalid/repo.git",
        repo_branch="scarthgap_11.00.09.04_var01",
        container_image="jetm/kas-build-env:5.2-f40",
    )


def test_build_env_emits_ccache_mount(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KAS_RUNTIME_ARGS", raising=False)
    cfg = _make_cfg(tmp_path)

    env = _build_env(cfg)

    expected = f"-v {tmp_path / 'ccache'}:/work/ccache:rw"
    assert env["KAS_RUNTIME_ARGS"] == expected
    assert (tmp_path / "ccache").is_dir()


def test_build_env_appends_to_existing_runtime_args(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KAS_RUNTIME_ARGS", "--cap-add=SYS_PTRACE")
    cfg = _make_cfg(tmp_path)

    env = _build_env(cfg)

    expected_mount = f"-v {tmp_path / 'ccache'}:/work/ccache:rw"
    assert env["KAS_RUNTIME_ARGS"] == f"--cap-add=SYS_PTRACE {expected_mount}"


def test_build_env_preserves_kas_work_dir_per_bsp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Ccache mount addition must not change KAS_WORK_DIR scoping."""
    monkeypatch.delenv("KAS_RUNTIME_ARGS", raising=False)
    cfg_ti = _make_cfg(tmp_path, bsp_family="ti")
    cfg_nxp = _make_cfg(tmp_path, bsp_family="nxp")

    env_ti = _build_env(cfg_ti)
    env_nxp = _build_env(cfg_nxp)

    assert env_ti["KAS_WORK_DIR"].endswith("/ti")
    assert env_nxp["KAS_WORK_DIR"].endswith("/nxp")
    # Both point at the same workspace-root ccache.
    assert env_ti["KAS_RUNTIME_ARGS"] == env_nxp["KAS_RUNTIME_ARGS"]
