"""``dl_mirror_url`` / ``cache_push_target`` thread from config.toml onto ``BuildConfig``."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from bakar.config import ResolveRequest, resolve
from bakar.user_config import load_user_config

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


def _resolve(workspace: Path, cfg_file: Path):
    return resolve(
        ResolveRequest(
            workspace=workspace,
            bsp_family="generic",
            user_config=load_user_config(cfg_file),
        )
    )


def test_both_keys_resolve_onto_build_config(tmp_path: Path, user_config_file: Path) -> None:
    user_config_file.write_text(
        '[build]\ndl_mirror_url = "http://hub:18742/downloads"\n'
        'cache_push_target = "pc1:/mnt/yocto-cache/downloads-incoming"\n'
    )
    cfg = _resolve(tmp_path, user_config_file)
    assert cfg.dl_mirror_url == "http://hub:18742/downloads"
    assert cfg.cache_push_target == "pc1:/mnt/yocto-cache/downloads-incoming"
    assert cfg.use_hub_mirror is True


def test_unset_keys_are_none(tmp_path: Path, user_config_file: Path) -> None:
    user_config_file.write_text('[build]\nsstate_mirror_url = "http://x"\n')
    cfg = _resolve(tmp_path, user_config_file)
    assert cfg.dl_mirror_url is None
    assert cfg.cache_push_target is None
    assert cfg.use_hub_mirror is False
