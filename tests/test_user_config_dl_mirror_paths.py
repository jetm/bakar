"""The optional ``[build]`` key ``dl_mirror_paths``: whitespace-separated absolute directories."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from bakar.user_config import SETTINGS_SCHEMA, load_user_config

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


def test_two_directories_load_in_order(user_config_file: Path) -> None:
    user_config_file.write_text('[build]\ndl_mirror_paths = "/mnt/a /mnt/b"\n')
    cfg = load_user_config(user_config_file)
    assert cfg.dl_mirror_paths is not None
    assert cfg.dl_mirror_paths.split() == ["/mnt/a", "/mnt/b"]


def test_omitted_key_is_none(user_config_file: Path) -> None:
    user_config_file.write_text('[build]\nsstate_mirror_url = "http://x"\n')
    assert load_user_config(user_config_file).dl_mirror_paths is None


@pytest.mark.parametrize(
    ("value", "offender"),
    [
        ("/mnt/a relative/dir", "relative/dir"),
        ("/mnt/a /mnt/../etc", "/mnt/../etc"),
        ("   ", "at least one absolute directory"),
    ],
)
def test_invalid_value_raises(user_config_file: Path, value: str, offender: str) -> None:
    user_config_file.write_text(f'[build]\ndl_mirror_paths = "{value}"\n')
    with pytest.raises(ValueError, match="dl_mirror_paths") as excinfo:
        load_user_config(user_config_file)
    assert offender in str(excinfo.value)


def test_key_in_settings_schema() -> None:
    assert "build.dl_mirror_paths" in SETTINGS_SCHEMA
