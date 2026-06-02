"""Tests for :class:`bakar.config.BuildConfig` resolution.

Covers fields not exercised by ``tests/test_env_precedence.py`` -- in
particular the ``use_hashequiv`` flag threaded from ``UserConfig.hashserv``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bakar.config import resolve
from bakar.user_config import UserConfig

pytestmark = pytest.mark.unit


def _workspace(tmp_path):
    """Return a workspace path with the nxp subdir present."""
    (tmp_path / "nxp").mkdir(parents=True, exist_ok=True)
    return tmp_path


def test_resolve_use_hashequiv_default_false_without_user_config(tmp_path) -> None:
    """Without a user_config, ``use_hashequiv`` resolves to False."""
    cfg = resolve(workspace=_workspace(tmp_path), bsp_family="nxp")

    assert cfg.use_hashequiv is False


def test_resolve_use_hashequiv_threads_from_user_config_true(tmp_path) -> None:
    """``UserConfig(hashserv=True)`` threads to ``cfg.use_hashequiv is True``."""
    uc = UserConfig(hashserv=True)

    cfg = resolve(workspace=_workspace(tmp_path), bsp_family="nxp", user_config=uc)

    assert cfg.use_hashequiv is True


def test_resolve_use_hashequiv_threads_from_user_config_false(tmp_path) -> None:
    """``UserConfig(hashserv=False)`` threads to ``cfg.use_hashequiv is False``."""
    uc = UserConfig(hashserv=False)

    cfg = resolve(workspace=_workspace(tmp_path), bsp_family="nxp", user_config=uc)

    assert cfg.use_hashequiv is False


def test_effective_ccache_dir_per_workspace_by_default(tmp_path) -> None:
    """Without opting in, ccache is per-workspace at ``<workspace>/ccache``."""
    ws = _workspace(tmp_path)
    cfg = resolve(workspace=ws, bsp_family="nxp")

    assert cfg.effective_ccache_dir == ws.resolve() / "ccache"


def test_effective_ccache_dir_shared_uses_xdg_cache(tmp_path, monkeypatch) -> None:
    """``ccache_shared`` selects a single shared cache under XDG_CACHE_HOME."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    uc = UserConfig(ccache_shared=True)

    cfg = resolve(workspace=_workspace(tmp_path), bsp_family="nxp", user_config=uc)

    assert cfg.effective_ccache_dir == tmp_path / "xdg" / "bakar" / "ccache"


def test_effective_ccache_dir_explicit_path_wins(tmp_path) -> None:
    """An explicit ``ccache_dir`` is honored verbatim, over shared and default."""
    uc = UserConfig(ccache_shared=True, ccache_dir="/mnt/cache/cc")

    cfg = resolve(workspace=_workspace(tmp_path), bsp_family="nxp", user_config=uc)

    assert cfg.effective_ccache_dir == Path("/mnt/cache/cc")
