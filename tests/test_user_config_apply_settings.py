"""Unit tests for bakar.user_config.apply_settings."""

from __future__ import annotations

import tomllib
from typing import TYPE_CHECKING

import pytest

from bakar.user_config import apply_settings, set_setting

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


def _read(path: Path) -> dict[str, object]:
    return tomllib.loads(path.read_text())


def test_set_and_unset_in_one_call(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    set_setting("build.ccache", "true", cfg)
    set_setting("build.capture_graph", "true", cfg)
    apply_settings({"build.ccache": "false"}, ["build.capture_graph"], cfg)
    build = _read(cfg)["build"]
    assert build == {"ccache": False}


def test_invalid_template_leaves_file_untouched(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    set_setting("build.ccache", "false", cfg)
    before = cfg.read_bytes()
    with pytest.raises(ValueError, match="nope"):
        apply_settings({"build.ccache": "true", "workspaces.template": "/x/{nope}"}, path=cfg)
    assert cfg.read_bytes() == before


def test_unknown_key_raises_before_write(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    with pytest.raises(ValueError, match=r"bogus\.key"):
        apply_settings({"build.ccache": "true", "bogus.key": "1"}, path=cfg)
    assert not cfg.exists()
    with pytest.raises(ValueError, match=r"bogus\.key"):
        apply_settings({}, ["bogus.key"], cfg)
    assert not cfg.exists()


def test_empty_call_creates_no_file(tmp_path: Path) -> None:
    cfg = tmp_path / "sub" / "config.toml"
    apply_settings({}, [], cfg)
    assert not cfg.exists()
    assert not cfg.parent.exists()


def test_unset_prunes_emptied_table(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    set_setting("dispatch.worktree_root", "/srv/wt", cfg)
    set_setting("build.ccache", "true", cfg)
    apply_settings({}, ["dispatch.worktree_root"], cfg)
    data = _read(cfg)
    assert "dispatch" not in data
    assert "build" in data


def test_key_in_both_raises(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    with pytest.raises(ValueError, match=r"build\.ccache"):
        apply_settings({"build.ccache": "true"}, ["build.ccache"], cfg)
    assert not cfg.exists()


def test_unsets_alone_create_no_file(tmp_path: Path) -> None:
    cfg = tmp_path / "sub" / "config.toml"
    apply_settings({}, ["build.ccache"], cfg)
    assert not cfg.exists()
    assert not cfg.parent.exists()


def test_write_keeps_the_existing_file_mode(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    set_setting("build.ccache", "true", cfg)
    cfg.chmod(0o600)
    apply_settings({"build.sccache_dist": "true"}, [], cfg)
    assert cfg.stat().st_mode & 0o777 == 0o600
    set_setting("build.ccache", "false", cfg)
    assert cfg.stat().st_mode & 0o777 == 0o600


def test_stale_temp_file_never_decides_the_final_mode(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    set_setting("build.ccache", "true", cfg)
    cfg.chmod(0o600)
    stale = tmp_path / "config.toml.tmp"
    stale.write_text("leftover")
    stale.chmod(0o644)
    apply_settings({"build.sccache_dist": "true"}, [], cfg)
    assert cfg.stat().st_mode & 0o777 == 0o600
    assert not stale.exists()
