"""Tests for :class:`bakar.config.BuildConfig` resolution.

Covers fields not exercised by ``tests/test_env_precedence.py`` -- in
particular the ``use_hashequiv`` flag threaded from ``UserConfig.hashserv``.
"""

from __future__ import annotations

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
