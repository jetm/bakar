"""Tests for the mold-linker config plumbing (task 3.1).

Covers the ``BuildConfig.mold``/``mold_mode`` fields, the ``UserConfig.mold``
config tier, and ``resolve()``'s accelerator-tier precedence (CLI > BAKAR_MOLD
env > [build] mold config > default off) plus the ``--mold`` / ``--mold-baseline``
mutual exclusion. The ``resolve`` keyword groups these tests for the task's
verify command.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from bakar.config import resolve
from bakar.user_config import (
    _BOOL_FIELDS,
    _BUILD_KEYS,
    SETTINGS_SCHEMA,
    UserConfig,
    load_user_config,
)

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


def _nxp_workspace(tmp_path: Path) -> Path:
    """Return a workspace path with the nxp subdir present (resolve() needs it)."""
    (tmp_path / "nxp").mkdir(parents=True, exist_ok=True)
    return tmp_path


# ---------------------------------------------------------------------------
# UserConfig / config-file tier
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_config_mold_field_defaults_off() -> None:
    """An all-defaults UserConfig has mold disabled and typed as a real bool."""
    cfg = UserConfig()
    assert cfg.mold is False
    assert isinstance(cfg.mold, bool)


@pytest.mark.unit
def test_config_mold_true_loads_as_bool(tmp_path: Path) -> None:
    """`[build] mold = true` loads as a real boolean True."""
    config_file = tmp_path / "config.toml"
    config_file.write_text("[build]\nmold = true\n")

    cfg = load_user_config(config_file)

    assert cfg.mold is True
    assert isinstance(cfg.mold, bool)


@pytest.mark.unit
def test_config_mold_non_bool_raises_naming_field(tmp_path: Path) -> None:
    """A non-bool value for `mold` raises ValueError naming the field."""
    config_file = tmp_path / "config.toml"
    config_file.write_text('[build]\nmold = "yes"\n')

    with pytest.raises(ValueError, match="mold"):
        load_user_config(config_file)


@pytest.mark.unit
def test_config_mold_registered_in_type_sets() -> None:
    """The field belongs to the bool registry, the build map, and the settings schema."""
    assert "mold" in _BOOL_FIELDS
    assert _BUILD_KEYS["mold"] == "mold"
    assert "build.mold" in SETTINGS_SCHEMA
    assert SETTINGS_SCHEMA["build.mold"].is_bool is True


# ---------------------------------------------------------------------------
# resolve() accelerator-tier precedence and MOLD_MODE
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resolve_mold_default_off(tmp_path: Path) -> None:
    """resolve() with no inputs yields mold off in list mode (default-off rollback)."""
    cfg = resolve(workspace=_nxp_workspace(tmp_path), bsp_family="nxp")

    assert cfg.mold is False
    assert cfg.mold_mode == "list"


@pytest.mark.unit
def test_resolve_cli_mold_enables(tmp_path: Path) -> None:
    """The --mold CLI override enables mold in list mode."""
    cfg = resolve(workspace=_nxp_workspace(tmp_path), bsp_family="nxp", mold=True)

    assert cfg.mold is True
    assert cfg.mold_mode == "list"


@pytest.mark.unit
def test_resolve_cli_mold_overrides_disabling_user_config(tmp_path: Path) -> None:
    """--mold wins over a config-file `mold = false` (CLI is the top tier)."""
    uc = UserConfig(mold=False)

    cfg = resolve(workspace=_nxp_workspace(tmp_path), bsp_family="nxp", user_config=uc, mold=True)

    assert cfg.mold is True


@pytest.mark.unit
def test_resolve_env_mold_overrides_disabling_user_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """BAKAR_MOLD=1 wins over a config-file `mold = false` (env beats config)."""
    monkeypatch.setenv("BAKAR_MOLD", "1")
    uc = UserConfig(mold=False)

    cfg = resolve(workspace=_nxp_workspace(tmp_path), bsp_family="nxp", user_config=uc)

    assert cfg.mold is True


@pytest.mark.unit
def test_resolve_mold_from_user_config(tmp_path: Path) -> None:
    """A config-file `mold = true` threads through when no CLI/env override is set."""
    uc = UserConfig(mold=True)

    cfg = resolve(workspace=_nxp_workspace(tmp_path), bsp_family="nxp", user_config=uc)

    assert cfg.mold is True
    assert cfg.mold_mode == "list"


@pytest.mark.unit
def test_resolve_mold_baseline_sets_baseline_mode(tmp_path: Path) -> None:
    """--mold-baseline enables mold in the symmetric bfd baseline mode."""
    cfg = resolve(workspace=_nxp_workspace(tmp_path), bsp_family="nxp", mold_baseline=True)

    assert cfg.mold is True
    assert cfg.mold_mode == "baseline"


@pytest.mark.unit
def test_resolve_mold_and_baseline_together_rejected(tmp_path: Path) -> None:
    """--mold and --mold-baseline together is a hard error (mutually exclusive)."""
    with pytest.raises(ValueError, match="mutually exclusive"):
        resolve(workspace=_nxp_workspace(tmp_path), bsp_family="nxp", mold=True, mold_baseline=True)
