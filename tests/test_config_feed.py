"""Tests for local package feed root resolution on :class:`bakar.config.BuildConfig`.

Mirrors the ``ccache_dir`` / ``ccache_shared`` trio in ``tests/test_config.py``:
per-workspace by default, an opt-in shared location, and an explicit path that
wins over both. The feed differs from ccache in one respect only - it is derived
data rather than a cache, so the shared location sits under the XDG *data* home
instead of the cache home.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bakar.config import ResolveRequest, resolve, shared_feed_dir
from bakar.user_config import UserConfig

pytestmark = pytest.mark.unit


def _workspace(tmp_path):
    """Return a workspace path with the nxp subdir present."""
    (tmp_path / "nxp").mkdir(parents=True, exist_ok=True)
    return tmp_path


def test_effective_feed_dir_per_workspace_by_default(tmp_path) -> None:
    """Without opting in, the feed is per-workspace at ``<workspace>/_feed``."""
    ws = _workspace(tmp_path)
    cfg = resolve(ResolveRequest(workspace=ws, bsp_family="nxp"))

    assert cfg.effective_feed_dir == ws.resolve() / "_feed"


def test_effective_feed_dir_shared_uses_xdg_data(tmp_path, monkeypatch) -> None:
    """``feed_shared`` selects a single shared feed under XDG_DATA_HOME."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    uc = UserConfig(feed_shared=True)

    cfg = resolve(ResolveRequest(workspace=_workspace(tmp_path), bsp_family="nxp", user_config=uc))

    assert cfg.effective_feed_dir == tmp_path / "xdg" / "bakar" / "feed"


def test_effective_feed_dir_explicit_path_wins(tmp_path) -> None:
    """An explicit ``feed_dir`` is honored verbatim, over shared and default.

    This is the path that carries the real deployment: the canonical feed lives
    on a large volume named here, not on whichever filesystem the workspace or
    the XDG data home happens to sit on.
    """
    uc = UserConfig(feed_shared=True, feed_dir="/mnt/big/avocado-feed")

    cfg = resolve(ResolveRequest(workspace=_workspace(tmp_path), bsp_family="nxp", user_config=uc))

    assert cfg.effective_feed_dir == Path("/mnt/big/avocado-feed")


def test_effective_feed_dir_expands_user_in_explicit_path(tmp_path) -> None:
    """A ``~`` in an explicit ``feed_dir`` is expanded, not taken literally.

    The deployment reaches the feed through a symlink under the home directory,
    so a literal ``~`` would produce a directory named ``~`` in the workspace.
    """
    uc = UserConfig(feed_dir="~/yocto-cache/shared-workspace/_feed")

    cfg = resolve(ResolveRequest(workspace=_workspace(tmp_path), bsp_family="nxp", user_config=uc))

    assert cfg.effective_feed_dir == Path.home() / "yocto-cache/shared-workspace/_feed"


def test_shared_feed_dir_returns_none_when_neither_is_set() -> None:
    """The helper returns None so the caller falls back to per-workspace.

    Asserted directly rather than only through ``effective_feed_dir`` because
    the None is the signal the property branches on; a helper that returned a
    path here would silently make every workspace share one feed.
    """
    assert shared_feed_dir(None, feed_shared=False) is None


def test_shared_feed_dir_explicit_path_beats_shared_flag(tmp_path, monkeypatch) -> None:
    """With both set the helper returns the explicit path, not the shared one."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))

    resolved = shared_feed_dir("/mnt/big/avocado-feed", feed_shared=True)

    assert resolved == Path("/mnt/big/avocado-feed")


def test_user_config_feed_fields_default_to_unset() -> None:
    """A bare ``UserConfig`` leaves both feed fields at their inert defaults.

    Guards the construction sites: a field added to ``UserConfig`` without a
    default would make every existing caller that omits it raise.
    """
    uc = UserConfig()

    assert uc.feed_dir is None
    assert uc.feed_shared is False
