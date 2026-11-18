"""``dl_mirror_paths`` threads from config.toml onto ``BuildConfig``."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from bakar.config import ResolveRequest, resolve
from bakar.user_config import load_user_config

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit

PATHS = "/mnt/hub/downloads:/mnt/hub/downloads-incoming"


def _resolve(workspace: Path, cfg_file: Path):
    return resolve(
        ResolveRequest(
            workspace=workspace,
            bsp_family="generic",
            user_config=load_user_config(cfg_file),
        )
    )


def test_paths_resolve_onto_build_config_unchanged(tmp_path: Path, user_config_file: Path) -> None:
    user_config_file.write_text(f'[build]\ndl_mirror_paths = "{PATHS}"\n')
    cfg = _resolve(tmp_path, user_config_file)
    assert cfg.dl_mirror_paths == PATHS


def test_absent_key_resolves_to_none(tmp_path: Path, user_config_file: Path) -> None:
    user_config_file.write_text('[build]\nsstate_mirror_url = "http://x"\n')
    cfg = _resolve(tmp_path, user_config_file)
    assert cfg.dl_mirror_paths is None
    assert cfg.use_hub_mount is False


def test_use_hub_mount_true_in_host_mode_with_key(tmp_path: Path, user_config_file: Path) -> None:
    user_config_file.write_text(f'[build]\ndl_mirror_paths = "{PATHS}"\n')
    cfg = replace(_resolve(tmp_path, user_config_file), host_mode=True)
    assert cfg.use_hub_mount is True


def test_use_hub_mount_false_in_container_mode(tmp_path: Path, user_config_file: Path) -> None:
    user_config_file.write_text(f'[build]\ndl_mirror_paths = "{PATHS}"\n')
    cfg = replace(_resolve(tmp_path, user_config_file), host_mode=False)
    assert cfg.dl_mirror_paths == PATHS
    assert cfg.use_hub_mount is False
