"""Tests for sccache-dist distributed-compile support.

Task 1.1 covers config plumbing in ``bakar.user_config``: the ``sccache_dist``
bool and ``sccache_scheduler_url`` string keys parse from ``[build]``, default
to their unset values, and a non-bool ``sccache_dist`` raises a typed error
naming the field. The ``config`` keyword groups these tests for the task's
verify command.
"""

from __future__ import annotations

import textwrap
from typing import TYPE_CHECKING

import pytest

from bakar.user_config import (
    _BOOL_FIELDS,
    _BUILD_KEYS,
    _STR_FIELDS,
    SETTINGS_SCHEMA,
    UserConfig,
    load_user_config,
)

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


@pytest.mark.unit
def test_config_sccache_fields_default_unset() -> None:
    """An all-defaults UserConfig has sccache disabled and no scheduler URL."""
    cfg = UserConfig()
    assert cfg.sccache_dist is False
    assert isinstance(cfg.sccache_dist, bool)
    assert cfg.sccache_scheduler_url is None


@pytest.mark.unit
def test_config_sccache_fields_absent_yield_defaults(tmp_path: Path) -> None:
    """A [build] table omitting both keys leaves the defaults in place."""
    config_file = tmp_path / "config.toml"
    config_file.write_text("[build]\ndoctor = true\n")

    cfg = load_user_config(config_file)

    assert cfg.sccache_dist is False
    assert cfg.sccache_scheduler_url is None


@pytest.mark.unit
def test_config_sccache_dist_true_loads_as_bool(tmp_path: Path) -> None:
    """`[build] sccache_dist = true` loads as a real boolean True."""
    config_file = tmp_path / "config.toml"
    config_file.write_text("[build]\nsccache_dist = true\n")

    cfg = load_user_config(config_file)

    assert cfg.sccache_dist is True
    assert isinstance(cfg.sccache_dist, bool)


@pytest.mark.unit
def test_config_sccache_scheduler_url_loads_as_str(tmp_path: Path) -> None:
    """A valid scheduler URL parses to a non-None string (falsifier guard)."""
    config_file = tmp_path / "config.toml"
    config_file.write_text('[build]\nsccache_dist = true\nsccache_scheduler_url = "http://localhost:10600"\n')

    cfg = load_user_config(config_file)

    assert cfg.sccache_scheduler_url == "http://localhost:10600"
    assert isinstance(cfg.sccache_scheduler_url, str)


@pytest.mark.unit
def test_config_sccache_dist_non_bool_raises_naming_field(tmp_path: Path) -> None:
    """A non-bool value for `sccache_dist` raises ValueError naming the field."""
    toml_content = textwrap.dedent("""\
        [build]
        sccache_dist = "yes"
    """)
    config_file = tmp_path / "config.toml"
    config_file.write_text(toml_content)

    with pytest.raises(ValueError, match="sccache_dist"):
        load_user_config(config_file)


@pytest.mark.unit
def test_config_sccache_scheduler_url_non_str_raises_naming_field(tmp_path: Path) -> None:
    """A non-string value for `sccache_scheduler_url` raises naming the field."""
    config_file = tmp_path / "config.toml"
    config_file.write_text("[build]\nsccache_scheduler_url = 10600\n")

    with pytest.raises(ValueError, match="sccache_scheduler_url"):
        load_user_config(config_file)


@pytest.mark.unit
def test_config_sccache_fields_registered_in_type_sets() -> None:
    """The two fields belong to the correct type registries and the build map."""
    assert "sccache_dist" in _BOOL_FIELDS
    assert "sccache_scheduler_url" in _STR_FIELDS
    assert _BUILD_KEYS["sccache_dist"] == "sccache_dist"
    assert _BUILD_KEYS["sccache_scheduler_url"] == "sccache_scheduler_url"


@pytest.mark.unit
def test_config_sccache_keys_present_in_settings_schema() -> None:
    """Both dotted keys are recognized by the settings schema."""
    assert "build.sccache_dist" in SETTINGS_SCHEMA
    assert "build.sccache_scheduler_url" in SETTINGS_SCHEMA
    assert SETTINGS_SCHEMA["build.sccache_dist"].is_bool is True
    assert SETTINGS_SCHEMA["build.sccache_scheduler_url"].is_bool is False
