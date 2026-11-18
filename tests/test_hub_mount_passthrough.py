"""Tests for the BAKAR_DL_MIRROR_PATHS host-mode build-environment export."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from bakar.config import BuildConfig
from bakar.steps import cache_push
from bakar.steps import hub_mount as hub_mount_mod
from bakar.steps import kas_build as kas_build_mod

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit

_VAR = "BAKAR_DL_MIRROR_PATHS"


def _cfg(tmp_path: Path, *, paths: str | None, host_mode: bool = True) -> BuildConfig:
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
        host_mode=host_mode,
        dl_mirror_paths=paths,
    )


def _probe(states: dict[str, str]):
    def fake(dirs):
        return [hub_mount_mod.DirStatus(path=d, state=states[str(d)], detail="") for d in dirs]

    return fake


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_VAR, raising=False)
    monkeypatch.setattr(hub_mount_mod, "_PROBE_MEMO", {})


def test_ready_dirs_exported_space_separated_in_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hub_mount_mod, "probe_dirs", _probe({"/mnt/b": "ready", "/mnt/a": "ready"}))
    env = kas_build_mod._build_env(_cfg(tmp_path, paths="/mnt/b /mnt/a"), ensure_hashserv=False)
    assert env[_VAR] == "/mnt/b /mnt/a"


def test_dropped_dir_is_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hub_mount_mod, "probe_dirs", _probe({"/mnt/a": "ready", "/mnt/b": "unresponsive"}))
    env = kas_build_mod._build_env(_cfg(tmp_path, paths="/mnt/a /mnt/b"), ensure_hashserv=False)
    assert env[_VAR] == "/mnt/a"


def test_none_ready_leaves_variable_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hub_mount_mod, "probe_dirs", _probe({"/mnt/a": "missing"}))
    env = kas_build_mod._build_env(_cfg(tmp_path, paths="/mnt/a"), ensure_hashserv=False)
    assert _VAR not in env


def test_caller_value_does_not_survive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_VAR, "/elsewhere")
    monkeypatch.setattr(hub_mount_mod, "probe_dirs", _probe({"/mnt/a": "ready"}))
    env = kas_build_mod._build_env(_cfg(tmp_path, paths="/mnt/a"), ensure_hashserv=False)
    assert env[_VAR] == "/mnt/a"
    monkeypatch.setattr(hub_mount_mod, "probe_dirs", _probe({"/mnt/a": "missing"}))
    env = kas_build_mod._build_env(_cfg(tmp_path, paths="/mnt/a"), ensure_hashserv=False)
    assert _VAR not in env


def test_container_mode_has_no_variable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_VAR, "/elsewhere")
    monkeypatch.setattr(hub_mount_mod, "probe_dirs", _probe({"/mnt/a": "ready"}))
    cfg = _cfg(tmp_path, paths="/mnt/a", host_mode=False)
    assert _VAR not in kas_build_mod._build_env(cfg, ensure_hashserv=False)
    assert _VAR not in " ".join(kas_build_mod._ccache_args(cfg, dry_run=True))


def test_key_unset_leaves_out_without_probing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(dirs):
        raise AssertionError("probed")

    monkeypatch.setattr(hub_mount_mod, "probe_dirs", boom)
    env = kas_build_mod._build_env(_cfg(tmp_path, paths=None), ensure_hashserv=False)
    assert _VAR not in env


def test_linked_payload_is_not_pushed(tmp_path: Path) -> None:
    real = tmp_path / "real.tar.gz"
    real.write_bytes(b"x")
    (tmp_path / "pkg.tar.gz").symlink_to(real)
    (tmp_path / "pkg.tar.gz.done").write_bytes(b"")
    assert cache_push.eligible_pairs(tmp_path, ["pkg.tar.gz.done"]) == []
    assert os.path.islink(tmp_path / "pkg.tar.gz")
