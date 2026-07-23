"""``_clean_build_dir`` must also wipe a host-mode ``local_tmpdir_base`` override.

The override relocates the build TMPDIR to node-local disk, outside the build
directory; a clean that only removes ``bsp_root/build_dir_name`` would strand
the redirected (potentially ~200G) tmp.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from bakar.commands._helpers import _clean_build_dir
from bakar.config import BuildConfig

if TYPE_CHECKING:
    from pathlib import Path


def _cfg(workspace: Path, *, local_tmpdir_base: str | None, host_mode: bool) -> BuildConfig:
    return BuildConfig(
        workspace=workspace,
        bsp_family="nxp",
        machine="imx8mp-var-dart",
        distro="fsl-imx-wayland",
        image="core-image-minimal",
        manifest="",
        repo_url="",
        repo_branch="scarthgap",
        kas_container_image="",
        host_mode=host_mode,
        local_tmpdir_base=local_tmpdir_base,
    )


@pytest.mark.unit
def test_clean_build_dir_removes_override_tmpdir(tmp_path: Path) -> None:
    """An active override -> the out-of-tree resolved TMPDIR is removed too."""
    workspace = tmp_path / "ws"
    local_base = tmp_path / "local-tmp"
    cfg = _cfg(workspace, local_tmpdir_base=str(local_base), host_mode=True)

    build_dir = cfg.bsp_root / cfg.build_dir_name
    (build_dir / "conf").mkdir(parents=True)
    tmpdir = cfg.resolved_tmpdir
    (tmpdir / "work").mkdir(parents=True)
    assert not tmpdir.is_relative_to(build_dir)

    _clean_build_dir(cfg)

    assert not build_dir.exists()
    assert not tmpdir.exists()


@pytest.mark.unit
def test_clean_build_dir_knob_unset_only_removes_build_dir(tmp_path: Path) -> None:
    """No override -> resolved TMPDIR sits under build_dir; nothing external is touched."""
    workspace = tmp_path / "ws"
    sibling = workspace / "nxp" / "keep-me"
    sibling.mkdir(parents=True)
    cfg = _cfg(workspace, local_tmpdir_base=None, host_mode=True)

    build_dir = cfg.bsp_root / cfg.build_dir_name
    (build_dir / "tmp").mkdir(parents=True)
    assert cfg.resolved_tmpdir.is_relative_to(build_dir)

    _clean_build_dir(cfg)

    assert not build_dir.exists()
    assert sibling.exists()
