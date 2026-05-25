"""Tests for the bbsetup family in :func:`bspctl.config.resolve`.

The bbsetup family routes its workspace (the bitbake-setup setup dir)
straight to ``bsp_root``, so the existing path properties yield
``<setup-dir>/kas-bbsetup.yml`` and ``<setup-dir>/build/runs`` without
any bbsetup-specific path logic.
"""

from __future__ import annotations

from bspctl.config import resolve


def test_resolve_bbsetup_does_not_raise(tmp_path):
    """resolve() must accept the bbsetup family literal without raising."""
    cfg = resolve(workspace=tmp_path, bsp_family="bbsetup")

    assert cfg.bsp_family == "bbsetup"


def test_bbsetup_bsp_root_is_workspace(tmp_path):
    """For bbsetup the setup dir IS the bsp root (no per-family subdir)."""
    cfg = resolve(workspace=tmp_path, bsp_family="bbsetup")

    assert cfg.bsp_root == tmp_path.resolve()


def test_bbsetup_default_kas_yaml(tmp_path):
    """default_kas_yaml lands at <setup-dir>/kas-bbsetup.yml."""
    cfg = resolve(workspace=tmp_path, bsp_family="bbsetup")

    assert cfg.default_kas_yaml.name == "kas-bbsetup.yml"
    assert cfg.default_kas_yaml == tmp_path.resolve() / "kas-bbsetup.yml"


def test_bbsetup_runs_dir(tmp_path):
    """runs_dir lands at <setup-dir>/build/runs."""
    cfg = resolve(workspace=tmp_path, bsp_family="bbsetup")

    assert cfg.runs_dir == tmp_path.resolve() / "build" / "runs"
