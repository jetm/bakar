"""Tests for the shared-cache opt-in overlay wiring.

Covers:
  (a) BuildConfig.use_shared_cache derived property.
  (b) _shared_cache_extra_overlays() returns overlay path when enabled, [] otherwise.
  (c) _build_env() includes BAKAR_SSTATE_MIRROR_URL when the URL is set; omits it
      when None.
  (d) CLI help text: `bakar build --help` shows --sstate-mirror; `bakar sync --help`
      does not.
  (e) UserConfig raises ValueError for a non-string sstate_mirror_url.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _plain(text: str) -> str:
    """Strip ANSI SGR escapes so help-text assertions survive colored output."""
    return _ANSI_RE.sub("", text)


# ---------------------------------------------------------------------------
# (a) BuildConfig.use_shared_cache
# ---------------------------------------------------------------------------


def test_use_shared_cache_true_when_url_set() -> None:
    """use_shared_cache is True when sstate_mirror_url is provided."""
    from bakar.config import BuildConfig

    cfg = BuildConfig(
        workspace=Path("/tmp"),
        bsp_family="nxp",
        machine="m",
        distro="d",
        image="i",
        manifest="x.xml",
        repo_url="https://example.com",
        repo_branch="main",
        container_image="img:latest",
        sstate_mirror_url="https://cache.example.com",
    )
    assert cfg.use_shared_cache is True


def test_use_shared_cache_false_when_url_not_set() -> None:
    """use_shared_cache is False when sstate_mirror_url is None (the default)."""
    from bakar.config import BuildConfig

    cfg = BuildConfig(
        workspace=Path("/tmp"),
        bsp_family="nxp",
        machine="m",
        distro="d",
        image="i",
        manifest="x.xml",
        repo_url="https://example.com",
        repo_branch="main",
        container_image="img:latest",
    )
    assert cfg.use_shared_cache is False


# ---------------------------------------------------------------------------
# (b) _shared_cache_extra_overlays
# ---------------------------------------------------------------------------


def _make_cfg(*, sstate_mirror_url: str | None = None) -> object:
    """Return a minimal BuildConfig for overlay helper tests."""
    from bakar.config import BuildConfig

    return BuildConfig(
        workspace=Path("/tmp"),
        bsp_family="nxp",
        machine="m",
        distro="d",
        image="i",
        manifest="x.xml",
        repo_url="https://example.com",
        repo_branch="main",
        container_image="img:latest",
        sstate_mirror_url=sstate_mirror_url,
    )


def test_shared_cache_extra_overlays_returns_path_when_enabled() -> None:
    """When use_shared_cache is True the helper returns a single-element list."""
    from bakar.commands._helpers import _shared_cache_extra_overlays

    cfg = _make_cfg(sstate_mirror_url="https://cache.example.com")
    result = _shared_cache_extra_overlays(cfg)  # type: ignore[arg-type]

    assert len(result) == 1
    assert result[0].name == "bakar-tuning-shared-cache.yml"
    assert result[0].is_file(), "overlay file must exist in the installed overlays/ dir"


def test_shared_cache_extra_overlays_returns_empty_when_disabled() -> None:
    """When use_shared_cache is False the helper returns an empty list."""
    from bakar.commands._helpers import _shared_cache_extra_overlays

    cfg = _make_cfg(sstate_mirror_url=None)
    result = _shared_cache_extra_overlays(cfg)  # type: ignore[arg-type]

    assert result == []


# ---------------------------------------------------------------------------
# (c) _build_env includes / omits BAKAR_SSTATE_MIRROR_URL
# ---------------------------------------------------------------------------


def test_build_env_includes_sstate_mirror_url_when_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BAKAR_SSTATE_MIRROR_URL is present in the env when sstate_mirror_url is set."""
    from bakar.config import BuildConfig
    from bakar.steps import kas_build as kas_build_mod

    cfg = BuildConfig(
        workspace=tmp_path,
        bsp_family="nxp",
        machine="m",
        distro="d",
        image="i",
        manifest="x.xml",
        repo_url="https://example.com",
        repo_branch="main",
        container_image="img:latest",
        sstate_mirror_url="https://cache.example.com",
    )

    # ensure_hashserv would try to start a daemon; patch it to a no-op
    monkeypatch.setattr(kas_build_mod.hashserv, "ensure_running", lambda _root: None)

    env = kas_build_mod._build_env(cfg, ensure_hashserv=False)

    assert "BAKAR_SSTATE_MIRROR_URL" in env
    assert env["BAKAR_SSTATE_MIRROR_URL"] == "https://cache.example.com"


def test_build_env_omits_sstate_mirror_url_when_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BAKAR_SSTATE_MIRROR_URL is absent from the env when sstate_mirror_url is None."""
    from bakar.config import BuildConfig
    from bakar.steps import kas_build as kas_build_mod

    cfg = BuildConfig(
        workspace=tmp_path,
        bsp_family="nxp",
        machine="m",
        distro="d",
        image="i",
        manifest="x.xml",
        repo_url="https://example.com",
        repo_branch="main",
        container_image="img:latest",
    )

    monkeypatch.setattr(kas_build_mod.hashserv, "ensure_running", lambda _root: None)

    # Also strip BAKAR_SSTATE_MIRROR_URL from the process environment so the
    # passthrough prefix loop cannot inject it.
    monkeypatch.delenv("BAKAR_SSTATE_MIRROR_URL", raising=False)

    env = kas_build_mod._build_env(cfg, ensure_hashserv=False)

    assert "BAKAR_SSTATE_MIRROR_URL" not in env


# ---------------------------------------------------------------------------
# (d) CLI help text
# ---------------------------------------------------------------------------


def test_build_help_shows_sstate_mirror() -> None:
    """`bakar build --help` must expose --sstate-mirror."""
    from typer.testing import CliRunner

    from bakar.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["build", "--help"])

    assert result.exit_code == 0, result.output
    assert "--sstate-mirror" in _plain(result.output)


def test_sync_help_does_not_show_sstate_mirror() -> None:
    """`bakar sync --help` must NOT expose --sstate-mirror."""
    from typer.testing import CliRunner

    from bakar.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["sync", "--help"])

    assert result.exit_code == 0, result.output
    assert "--sstate-mirror" not in _plain(result.output)


# ---------------------------------------------------------------------------
# (e) UserConfig raises ValueError for non-string sstate_mirror_url
# ---------------------------------------------------------------------------


def test_user_config_rejects_non_string_sstate_mirror_url(tmp_path: Path) -> None:
    """load_user_config raises ValueError when sstate_mirror_url is not a string."""
    import tomli_w

    from bakar.user_config import load_user_config

    config_file = tmp_path / "config.toml"
    with config_file.open("wb") as f:
        tomli_w.dump({"build": {"sstate_mirror_url": 42}}, f)

    with pytest.raises(ValueError, match="sstate_mirror_url"):
        load_user_config(config_file)


def test_user_config_accepts_string_sstate_mirror_url(tmp_path: Path) -> None:
    """load_user_config succeeds when sstate_mirror_url is a valid string."""
    import tomli_w

    from bakar.user_config import load_user_config

    config_file = tmp_path / "config.toml"
    with config_file.open("wb") as f:
        tomli_w.dump({"build": {"sstate_mirror_url": "https://cache.example.com"}}, f)

    cfg = load_user_config(config_file)
    assert cfg.sstate_mirror_url == "https://cache.example.com"
