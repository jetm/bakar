"""Tests for the BAKAR_DL_MIRROR_URL build-environment passthrough."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from bakar.config import BuildConfig
from bakar.steps import kas_build as kas_build_mod

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


def _cfg(tmp_path: Path, *, dl_mirror_url: str | None = None) -> BuildConfig:
    return BuildConfig(
        workspace=tmp_path,
        bsp_family="nxp",
        machine="m",
        distro="d",
        image="i",
        manifest="x.xml",
        repo_url="https://example.com",
        repo_branch="main",
        kas_container_image="img:latest",
        dl_mirror_url=dl_mirror_url,
    )


def test_env_carries_configured_dl_mirror_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BAKAR_DL_MIRROR_URL", raising=False)
    env = kas_build_mod._build_env(
        _cfg(tmp_path, dl_mirror_url="https://hub.example.com/downloads"), ensure_hashserv=False
    )
    assert env["BAKAR_DL_MIRROR_URL"] == "https://hub.example.com/downloads"


def test_existing_env_value_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BAKAR_DL_MIRROR_URL", "https://from-env.example.com")
    env = kas_build_mod._build_env(
        _cfg(tmp_path, dl_mirror_url="https://hub.example.com/downloads"), ensure_hashserv=False
    )
    assert env["BAKAR_DL_MIRROR_URL"] == "https://from-env.example.com"


def test_key_absent_when_unset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BAKAR_DL_MIRROR_URL", raising=False)
    env = kas_build_mod._build_env(_cfg(tmp_path), ensure_hashserv=False)
    assert "BAKAR_DL_MIRROR_URL" not in env


def test_empty_env_value_is_treated_as_unset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BAKAR_DL_MIRROR_URL", "")
    env = kas_build_mod._build_env(
        _cfg(tmp_path, dl_mirror_url="https://hub.example.com/downloads"), ensure_hashserv=False
    )
    assert env["BAKAR_DL_MIRROR_URL"] == "https://hub.example.com/downloads"


def test_empty_env_value_without_config_is_dropped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BAKAR_DL_MIRROR_URL", "")
    env = kas_build_mod._build_env(_cfg(tmp_path), ensure_hashserv=False)
    assert "BAKAR_DL_MIRROR_URL" not in env


def test_container_args_forward_url_only_when_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BAKAR_DL_MIRROR_URL", raising=False)
    container = replace(_cfg(tmp_path), host_mode=False)
    unset = kas_build_mod._ccache_args(container, dry_run=True)
    assert "BAKAR_DL_MIRROR_URL" not in " ".join(unset)
    cfg = replace(_cfg(tmp_path, dl_mirror_url="https://hub.example.com/downloads"), host_mode=False)
    args = kas_build_mod._ccache_args(cfg, dry_run=True)
    assert "-e BAKAR_DL_MIRROR_URL=https://hub.example.com/downloads" in args[1]
    monkeypatch.setenv("BAKAR_DL_MIRROR_URL", "")
    args = kas_build_mod._ccache_args(container, dry_run=True)
    assert "BAKAR_DL_MIRROR_URL" not in " ".join(args)


def _container_args(tmp_path: Path, url: str) -> str:
    cfg = replace(_cfg(tmp_path, dl_mirror_url=url), host_mode=False)
    return " ".join(kas_build_mod._ccache_args(cfg, dry_run=False))


def test_ipv6_url_is_forwarded_verbatim_unquoted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BAKAR_DL_MIRROR_URL", raising=False)
    args = _container_args(tmp_path, "http://[fd00::1]:8080/dl")
    assert "-e BAKAR_DL_MIRROR_URL=http://[fd00::1]:8080/dl" in args
    assert "'" not in args


@pytest.mark.parametrize("url", ["http://hub/a b", "http://hub/$x", "http://hub/a?b=1", "http://hub/p[1]"])
def test_unforwardable_url_is_not_forwarded_and_warns_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], url: str
) -> None:
    monkeypatch.delenv("BAKAR_DL_MIRROR_URL", raising=False)
    args = _container_args(tmp_path, url)
    assert "BAKAR_DL_MIRROR_URL" not in args
    err = capsys.readouterr().err
    assert len([ln for ln in err.splitlines() if "hub mirror" in ln]) == 1
    assert url not in err


def test_normal_url_forwarded_without_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("BAKAR_DL_MIRROR_URL", raising=False)
    args = _container_args(tmp_path, "http://192.168.8.174:18742/downloads")
    assert "-e BAKAR_DL_MIRROR_URL=http://192.168.8.174:18742/downloads" in args
    assert capsys.readouterr().err == ""
