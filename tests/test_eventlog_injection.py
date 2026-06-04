"""Unit tests for BB_DEFAULT_EVENTLOG injection in bakar.steps.kas_build.

Covers the two halves of the central injection (design D2/D4):

- ``_build_env`` adds ``BB_DEFAULT_EVENTLOG`` only when an ``eventlog_path`` is
  supplied; env-rendering-only call sites that pass nothing keep the key absent.
- ``_container_eventlog_path`` maps a normal build's host run dir to a
  ``/work/...`` container path (KAS_WORK_DIR mounted at ``/work``), and returns
  the host path verbatim in ``host_mode`` (no container).

Direct calls only - no kas-container is invoked. Mirrors tests/test_kas_env.py
for the BuildConfig fixture style.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from bakar.config import BuildConfig
from bakar.observability import RunLogger
from bakar.steps.kas_build import _build_env, _container_eventlog_path

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


def _make_cfg(workspace: Path, bsp_family: str = "nxp", *, host_mode: bool = False) -> BuildConfig:
    return BuildConfig(
        workspace=workspace,
        bsp_family=bsp_family,  # type: ignore[arg-type]
        machine="imx8mp-var-dart",
        distro="fsl-imx-xwayland",
        image="core-image-minimal",
        manifest="imx-6.6.52-2.2.2.xml",
        repo_url="https://example.invalid/repo.git",
        repo_branch="imx-6.6.52-2.2.2",
        container_image="jetm/kas-build-env:5.2-f40",
        host_mode=host_mode,
    )


def test_build_env_injects_bb_default_eventlog_when_path_given(tmp_path: Path) -> None:
    """When eventlog_path is supplied, BB_DEFAULT_EVENTLOG carries it verbatim."""
    cfg = _make_cfg(tmp_path)
    path = "/work/build/runs/x/bitbake_eventlog.json"

    env = _build_env(cfg, eventlog_path=path)

    assert env["BB_DEFAULT_EVENTLOG"] == path


def test_build_env_omits_bb_default_eventlog_when_no_path(tmp_path: Path) -> None:
    """The env-rendering-only sites pass no path; the key must be absent."""
    cfg = _make_cfg(tmp_path)

    env = _build_env(cfg)

    assert "BB_DEFAULT_EVENTLOG" not in env


def test_container_eventlog_path_maps_run_dir_to_work_mount(tmp_path: Path) -> None:
    """A normal build's host run dir maps under /work (KAS_WORK_DIR=cfg.bsp_root)."""
    cfg = _make_cfg(tmp_path)
    log = RunLogger(runs_dir=cfg.runs_dir)

    container_path = _container_eventlog_path(cfg, log)

    assert container_path == f"/work/build/runs/{log.run_id}/bitbake_eventlog.json"


def test_container_eventlog_path_host_mode_uses_host_path(tmp_path: Path) -> None:
    """In host_mode there is no container, so the host path is used verbatim."""
    cfg = _make_cfg(tmp_path, host_mode=True)
    log = RunLogger(runs_dir=cfg.runs_dir)

    container_path = _container_eventlog_path(cfg, log)

    assert container_path == str(log.eventlog_path)
    assert not container_path.startswith("/work/")


def test_container_eventlog_path_run_dir_outside_mount_falls_back(tmp_path: Path) -> None:
    """A run dir outside the bind-mounted tree (bakar dump/lock use a temp run
    dir) must fall back to the host path instead of raising ValueError."""
    cfg = _make_cfg(tmp_path)
    outside = tmp_path / "elsewhere" / "runs"
    log = RunLogger(runs_dir=outside)

    container_path = _container_eventlog_path(cfg, log)

    assert container_path == str(log.eventlog_path)
