"""Precedence tests for the explicit ``host_mode`` config toggle.

The toggle resolves through ``config.resolve()`` with precedence
``CLI --host > BAKAR_HOST_MODE env > workspace [build] host_mode > user config
host_mode > auto-detect-when-no-container-image``. The CLI flag and an explicit
toggle can only force host ON; neither forces a container when no image is
configured, so the out-of-the-box host default survives.

The falsifier these tests defend: setting ``[build] host_mode = true`` with an
image configured must select host mode, and an unset toggle with no image
configured must auto-select host. A regression that drops the new key (silent
ignore) fails ``test_workspace_toggle_with_image_selects_host`` and
``test_unset_toggle_no_image_auto_selects_host``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from bakar.config import resolve
from bakar.user_config import UserConfig
from bakar.workspace_config import WorkspaceConfig

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip ambient KAS_CONTAINER_IMAGE / BAKAR_HOST_MODE so each test controls them."""
    monkeypatch.delenv("KAS_CONTAINER_IMAGE", raising=False)
    monkeypatch.delenv("BAKAR_HOST_MODE", raising=False)


def _workspace(tmp_path: Path) -> Path:
    """Return a workspace path with the nxp subdir present (resolve() needs it)."""
    (tmp_path / "nxp").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _with_image() -> UserConfig:
    """A user config that configures a container image (suppresses auto-detect)."""
    return UserConfig(kas_container_image="some/image:latest")


# --- Falsifier guards -----------------------------------------------------


@pytest.mark.unit
def test_workspace_toggle_with_image_selects_host(tmp_path: Path) -> None:
    """[build] host_mode = true with an image configured selects host mode.

    Direct falsifier guard: the workspace toggle must override the container
    auto-detect that an explicit image would otherwise satisfy.
    """
    cfg = resolve(
        workspace=_workspace(tmp_path),
        bsp_family="nxp",
        user_config=_with_image(),
        workspace_config=WorkspaceConfig(host_mode=True),
    )
    assert cfg.host_mode is True


@pytest.mark.unit
def test_unset_toggle_no_image_auto_selects_host(tmp_path: Path) -> None:
    """An unset toggle with no image configured auto-selects host mode.

    Direct falsifier guard: with no container image anywhere and no explicit
    toggle, resolution must fall through to host.
    """
    cfg = resolve(
        workspace=_workspace(tmp_path),
        bsp_family="nxp",
        user_config=UserConfig(),
        workspace_config=WorkspaceConfig(),
    )
    assert cfg.host_mode is True


# --- Container-default preservation ---------------------------------------


@pytest.mark.unit
def test_unset_toggle_with_image_uses_container(tmp_path: Path) -> None:
    """No toggle anywhere + an image configured keeps the container path."""
    cfg = resolve(
        workspace=_workspace(tmp_path),
        bsp_family="nxp",
        user_config=_with_image(),
        workspace_config=WorkspaceConfig(),
    )
    assert cfg.host_mode is False


@pytest.mark.unit
def test_explicit_false_toggle_with_image_uses_container(tmp_path: Path) -> None:
    """An explicit host_mode = false with an image declines to force host."""
    cfg = resolve(
        workspace=_workspace(tmp_path),
        bsp_family="nxp",
        user_config=_with_image(),
        workspace_config=WorkspaceConfig(host_mode=False),
    )
    assert cfg.host_mode is False


@pytest.mark.unit
def test_container_opt_in(tmp_path: Path) -> None:
    """Host is the default; the container is reachable only by configuring an image.

    Asserts both directions of the opt-in contract in one place:

    - Configuring a container image (and no host toggle) selects the container
      path, so the opt-in remains reachable without a code change.
    - Removing the image (the workspace default) falls through to host mode.

    The 1.2 falsifier defends against either half breaking: configuring an
    image not running a container, or removing the image not defaulting to host.
    """
    # Direction 1: an image is configured -> container path.
    container = resolve(
        workspace=_workspace(tmp_path),
        bsp_family="nxp",
        user_config=_with_image(),
        workspace_config=WorkspaceConfig(),
    )
    assert container.host_mode is False

    # Direction 2: no image configured anywhere -> host default.
    host = resolve(
        workspace=_workspace(tmp_path),
        bsp_family="nxp",
        user_config=UserConfig(),
        workspace_config=WorkspaceConfig(),
    )
    assert host.host_mode is True


@pytest.mark.unit
def test_explicit_false_toggle_no_image_still_auto_host(tmp_path: Path) -> None:
    """An explicit host_mode = false cannot force a container when no image exists.

    The toggle only ever forces host ON; a False value defers to auto-detect,
    which still picks host because no image is configured.
    """
    cfg = resolve(
        workspace=_workspace(tmp_path),
        bsp_family="nxp",
        user_config=UserConfig(),
        workspace_config=WorkspaceConfig(host_mode=False),
    )
    assert cfg.host_mode is True


# --- Precedence ordering --------------------------------------------------


@pytest.mark.unit
def test_cli_host_flag_wins_over_image(tmp_path: Path) -> None:
    """CLI --host (spec.host_mode) forces host even with an image configured."""
    from bakar.config import BSPSpec

    cfg = resolve(
        workspace=_workspace(tmp_path),
        bsp_family="nxp",
        spec=BSPSpec(host_mode=True),
        user_config=_with_image(),
        workspace_config=WorkspaceConfig(),
    )
    assert cfg.host_mode is True


@pytest.mark.unit
def test_env_toggle_wins_over_workspace_and_user(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """BAKAR_HOST_MODE=1 forces host, overriding a workspace/user toggle and an image."""
    monkeypatch.setenv("BAKAR_HOST_MODE", "1")
    cfg = resolve(
        workspace=_workspace(tmp_path),
        bsp_family="nxp",
        user_config=UserConfig(host_mode=False, kas_container_image="some/image:latest"),
        workspace_config=WorkspaceConfig(host_mode=False),
    )
    assert cfg.host_mode is True


@pytest.mark.unit
def test_env_false_overrides_workspace_true_toggle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """BAKAR_HOST_MODE=0 outranks a workspace host_mode=true; with an image -> container."""
    monkeypatch.setenv("BAKAR_HOST_MODE", "0")
    cfg = resolve(
        workspace=_workspace(tmp_path),
        bsp_family="nxp",
        user_config=_with_image(),
        workspace_config=WorkspaceConfig(host_mode=True),
    )
    assert cfg.host_mode is False


@pytest.mark.unit
def test_workspace_toggle_wins_over_user_toggle(tmp_path: Path) -> None:
    """Workspace host_mode=true outranks user host_mode=false (with an image)."""
    cfg = resolve(
        workspace=_workspace(tmp_path),
        bsp_family="nxp",
        user_config=UserConfig(host_mode=False, kas_container_image="some/image:latest"),
        workspace_config=WorkspaceConfig(host_mode=True),
    )
    assert cfg.host_mode is True


@pytest.mark.unit
def test_user_toggle_selects_host_with_image(tmp_path: Path) -> None:
    """A user config host_mode=true selects host even with an image (no higher tier set)."""
    cfg = resolve(
        workspace=_workspace(tmp_path),
        bsp_family="nxp",
        user_config=UserConfig(host_mode=True, kas_container_image="some/image:latest"),
        workspace_config=WorkspaceConfig(),
    )
    assert cfg.host_mode is True


# --- Workspace .bakar.toml parsing ----------------------------------------


@pytest.mark.unit
def test_workspace_toml_parses_host_mode_key(tmp_path: Path) -> None:
    """The [build] host_mode key round-trips through load_workspace_config.

    Guards the silent-ignore regression: if workspace_config drops the key it
    would warn-and-skip, leaving host_mode None and the build on the container
    path despite the image being configured.
    """
    from bakar.workspace_config import load_workspace_config

    (tmp_path / "nxp").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".bakar.toml").write_text(
        "[build]\nhost_mode = true\n",
        encoding="utf-8",
    )
    wc = load_workspace_config(tmp_path)
    assert wc.host_mode is True

    cfg = resolve(
        workspace=tmp_path,
        bsp_family="nxp",
        user_config=_with_image(),
        workspace_config=wc,
    )
    assert cfg.host_mode is True
