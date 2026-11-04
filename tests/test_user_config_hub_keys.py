"""The optional ``[build]`` hub-cache keys ``dl_mirror_url`` and ``cache_push_target``."""

from __future__ import annotations

import textwrap
from typing import TYPE_CHECKING

import pytest

from bakar.user_config import SETTINGS_SCHEMA, load_user_config

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


def test_both_keys_load_as_str(user_config_file: Path) -> None:
    user_config_file.write_text(
        textwrap.dedent(
            """\
            [build]
            dl_mirror_url = "http://192.168.8.174:18742/downloads"
            cache_push_target = "pc1:/mnt/yocto-cache/downloads-incoming"
            """
        )
    )
    cfg = load_user_config(user_config_file)
    assert cfg.dl_mirror_url == "http://192.168.8.174:18742/downloads"
    assert cfg.cache_push_target == "pc1:/mnt/yocto-cache/downloads-incoming"


def test_omitted_keys_are_none(user_config_file: Path) -> None:
    user_config_file.write_text('[build]\nsstate_mirror_url = "http://x"\n')
    cfg = load_user_config(user_config_file)
    assert cfg.dl_mirror_url is None
    assert cfg.cache_push_target is None


@pytest.mark.parametrize("dotted", ["build.dl_mirror_url", "build.cache_push_target"])
def test_keys_in_settings_schema(dotted: str) -> None:
    assert dotted in SETTINGS_SCHEMA
