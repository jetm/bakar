"""Precedence tests for build-mode resolution: host is the structural default.

bakar runs on the host by default; the kas-container path is opt-in. Mode
resolves through ``config.resolve()`` with precedence::

    CLI --container > CLI --host > BAKAR_CONTAINER env > workspace [build]
    container > user config container > host (structural default)

The ``host_mode`` toggle and ``--host`` flag are retained as no-op back-compat
aliases: they only ever forced host, which is now the default, so an existing
config carrying ``host_mode = true`` keeps working unchanged. Configuring a
``KAS_CONTAINER_IMAGE`` no longer auto-selects the container - only an explicit
container opt-in does.

The falsifier these tests defend: an unset config must select host; a
configured image with no container opt-in must STILL select host; and only an
explicit ``container`` toggle / ``--container`` / ``BAKAR_CONTAINER`` may select
the container path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from bakar.config import BSPSpec, resolve
from bakar.user_config import UserConfig
from bakar.workspace_config import WorkspaceConfig

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip ambient mode env vars so each test controls them explicitly."""
    monkeypatch.delenv("KAS_CONTAINER_IMAGE", raising=False)
    monkeypatch.delenv("BAKAR_HOST_MODE", raising=False)
    monkeypatch.delenv("BAKAR_CONTAINER", raising=False)


def _workspace(tmp_path: Path) -> Path:
    """Return a workspace path with the nxp subdir present (resolve() needs it)."""
    (tmp_path / "nxp").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _with_image() -> UserConfig:
    """A user config that configures a container image (no longer auto-detected)."""
    return UserConfig(kas_container_image="some/image:latest")


# --- Host is the structural default ---------------------------------------


@pytest.mark.unit
def test_unset_config_selects_host(tmp_path: Path) -> None:
    """No toggle anywhere selects host - the structural default, no config needed."""
    cfg = resolve(
        workspace=_workspace(tmp_path),
        bsp_family="nxp",
        user_config=UserConfig(),
        workspace_config=WorkspaceConfig(),
    )
    assert cfg.host_mode is True


@pytest.mark.unit
def test_configured_image_alone_still_selects_host(tmp_path: Path) -> None:
    """A configured KAS_CONTAINER_IMAGE no longer auto-selects the container.

    Direct falsifier guard: the old auto-detect (image present -> container) is
    gone, so an image without an explicit container opt-in must select host.
    """
    cfg = resolve(
        workspace=_workspace(tmp_path),
        bsp_family="nxp",
        user_config=_with_image(),
        workspace_config=WorkspaceConfig(),
    )
    assert cfg.host_mode is True


@pytest.mark.unit
def test_host_mode_toggle_is_noop_alias(tmp_path: Path) -> None:
    """The retained host_mode toggle only ever forces host; False does NOT mean container."""
    cfg = resolve(
        workspace=_workspace(tmp_path),
        bsp_family="nxp",
        user_config=UserConfig(host_mode=False),
        workspace_config=WorkspaceConfig(host_mode=False),
    )
    assert cfg.host_mode is True


# --- Container opt-in ------------------------------------------------------


@pytest.mark.unit
def test_user_container_toggle_selects_container(tmp_path: Path) -> None:
    """A user config container = true opts into the kas-container path."""
    cfg = resolve(
        workspace=_workspace(tmp_path),
        bsp_family="nxp",
        user_config=UserConfig(container=True),
        workspace_config=WorkspaceConfig(),
    )
    assert cfg.host_mode is False


@pytest.mark.unit
def test_workspace_container_toggle_selects_container(tmp_path: Path) -> None:
    """A workspace [build] container = true opts into the kas-container path."""
    cfg = resolve(
        workspace=_workspace(tmp_path),
        bsp_family="nxp",
        user_config=UserConfig(),
        workspace_config=WorkspaceConfig(container=True),
    )
    assert cfg.host_mode is False


@pytest.mark.unit
def test_cli_container_flag_selects_container(tmp_path: Path) -> None:
    """CLI --container (spec.container_mode) opts into the container path."""
    cfg = resolve(
        workspace=_workspace(tmp_path),
        bsp_family="nxp",
        spec=BSPSpec(container_mode=True),
        user_config=UserConfig(),
        workspace_config=WorkspaceConfig(),
    )
    assert cfg.host_mode is False


# --- Precedence ordering ---------------------------------------------------


@pytest.mark.unit
def test_cli_container_wins_over_cli_host(tmp_path: Path) -> None:
    """When both CLI flags are passed, --container wins (the affirmative request)."""
    cfg = resolve(
        workspace=_workspace(tmp_path),
        bsp_family="nxp",
        spec=BSPSpec(host_mode=True, container_mode=True),
        user_config=UserConfig(),
        workspace_config=WorkspaceConfig(),
    )
    assert cfg.host_mode is False


@pytest.mark.unit
def test_cli_host_flag_overrides_container_toggle(tmp_path: Path) -> None:
    """CLI --host forces host even when a config container toggle is set."""
    cfg = resolve(
        workspace=_workspace(tmp_path),
        bsp_family="nxp",
        spec=BSPSpec(host_mode=True),
        user_config=UserConfig(container=True),
        workspace_config=WorkspaceConfig(container=True),
    )
    assert cfg.host_mode is True


@pytest.mark.unit
def test_env_container_wins_over_workspace_and_user(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """BAKAR_CONTAINER=1 forces container, overriding workspace/user toggles."""
    monkeypatch.setenv("BAKAR_CONTAINER", "1")
    cfg = resolve(
        workspace=_workspace(tmp_path),
        bsp_family="nxp",
        user_config=UserConfig(container=False),
        workspace_config=WorkspaceConfig(container=False),
    )
    assert cfg.host_mode is False


@pytest.mark.unit
def test_env_container_false_overrides_workspace_true(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """BAKAR_CONTAINER=0 outranks a workspace container=true toggle -> host."""
    monkeypatch.setenv("BAKAR_CONTAINER", "0")
    cfg = resolve(
        workspace=_workspace(tmp_path),
        bsp_family="nxp",
        user_config=UserConfig(),
        workspace_config=WorkspaceConfig(container=True),
    )
    assert cfg.host_mode is True


@pytest.mark.unit
def test_workspace_container_wins_over_user_container(tmp_path: Path) -> None:
    """Workspace container=false outranks a user container=true toggle -> host."""
    cfg = resolve(
        workspace=_workspace(tmp_path),
        bsp_family="nxp",
        user_config=UserConfig(container=True),
        workspace_config=WorkspaceConfig(container=False),
    )
    assert cfg.host_mode is True


# --- Workspace .bakar.toml parsing -----------------------------------------


@pytest.mark.unit
def test_workspace_toml_parses_container_key(tmp_path: Path) -> None:
    """The [build] container key round-trips through load_workspace_config.

    Guards the silent-ignore regression: a dropped key would warn-and-skip,
    leaving container None and the build on the host path despite the opt-in.
    """
    from bakar.workspace_config import load_workspace_config

    (tmp_path / "nxp").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".bakar.toml").write_text("[build]\ncontainer = true\n", encoding="utf-8")
    wc = load_workspace_config(tmp_path)
    assert wc.container is True

    cfg = resolve(workspace=tmp_path, bsp_family="nxp", user_config=UserConfig(), workspace_config=wc)
    assert cfg.host_mode is False


@pytest.mark.unit
def test_workspace_toml_still_parses_host_mode_key(tmp_path: Path) -> None:
    """The retained [build] host_mode key still parses (back-compat, no error)."""
    from bakar.workspace_config import load_workspace_config

    (tmp_path / "nxp").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".bakar.toml").write_text("[build]\nhost_mode = true\n", encoding="utf-8")
    wc = load_workspace_config(tmp_path)
    assert wc.host_mode is True

    cfg = resolve(workspace=tmp_path, bsp_family="nxp", user_config=UserConfig(), workspace_config=wc)
    assert cfg.host_mode is True
