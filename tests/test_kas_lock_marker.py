"""Tests for the one-step ownership marker and the unreadable-marker refusal."""

from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest

from bakar import build_stop
from bakar.config import BuildConfig
from bakar.observability import RunLogger
from bakar.steps import kas_lock
from bakar.steps.kas_lock import LockHeldByPeerError, LockUnattributableError, lock_owner_marker


def _cfg(workspace: Path) -> BuildConfig:
    (workspace / "nxp").mkdir(parents=True, exist_ok=True)
    return BuildConfig(
        workspace=workspace,
        bsp_family="nxp",  # type: ignore[arg-type]
        machine="imx8mp-var-dart",
        distro="fsl-imx-xwayland",
        image="core-image-minimal",
        manifest="imx-6.6.52-2.2.2.xml",
        repo_url="https://example.invalid/repo.git",
        repo_branch="scarthgap",
        kas_container_image="jetm/kas-build-env:latest",
        host_mode=False,
    )


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[BuildConfig, Path, RunLogger]:
    cfg = _cfg(tmp_path)
    build = cfg.bsp_root / cfg.build_dir_name
    build.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(socket, "gethostname", lambda: "this-host")
    monkeypatch.setattr(kas_lock.time, "sleep", lambda _s: None)
    return cfg, build, RunLogger(runs_dir=cfg.runs_dir)


def _on_nfs(monkeypatch: pytest.MonkeyPatch, value: bool | None) -> None:
    monkeypatch.setattr("bakar.diagnostics.is_path_on_nfs", lambda _p: value)


def _break_read(monkeypatch: pytest.MonkeyPatch, marker: Path) -> None:
    real = Path.read_text

    def fake(self: Path, *a: object, **k: object) -> str:
        if self == marker:
            raise PermissionError("denied")
        return real(self, *a, **k)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", fake)


def test_marker_created_with_full_content_and_no_temp_left(env: tuple[BuildConfig, Path, RunLogger]) -> None:
    cfg, build, _log = env
    marker = build_stop.lock_marker_path(cfg)
    assert kas_lock._create_marker_exclusive(marker, "this-host") is True
    assert marker.read_text() == "this-host"
    assert [p.name for p in build.iterdir()] == [marker.name]


def test_marker_never_created_empty_via_open_x(
    env: tuple[BuildConfig, Path, RunLogger], monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, _build, log = env
    seen: list[str] = []
    real_link = os.link

    def spy(src: os.PathLike[str], dst: os.PathLike[str]) -> None:
        seen.append(Path(src).read_text())
        real_link(src, dst)

    monkeypatch.setattr(os, "link", spy)
    with lock_owner_marker(cfg, log):
        pass
    assert seen == ["this-host"]


def test_existing_marker_reports_false(env: tuple[BuildConfig, Path, RunLogger]) -> None:
    cfg, build, _log = env
    marker = build_stop.lock_marker_path(cfg)
    marker.write_text("peer")
    assert kas_lock._create_marker_exclusive(marker, "this-host") is False
    assert marker.read_text() == "peer"
    assert [p.name for p in build.iterdir()] == [marker.name]


def test_link_lost_reply_with_nlink_two_is_success(
    env: tuple[BuildConfig, Path, RunLogger], monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, _build, _log = env
    marker = build_stop.lock_marker_path(cfg)
    real_link = os.link

    def lost_reply(src: os.PathLike[str], dst: os.PathLike[str]) -> None:
        real_link(src, dst)
        raise OSError(5, "reply lost")

    monkeypatch.setattr(os, "link", lost_reply)
    assert kas_lock._create_marker_exclusive(marker, "this-host") is True
    assert marker.read_text() == "this-host"


def test_link_failure_with_nlink_one_raises(
    env: tuple[BuildConfig, Path, RunLogger], monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, build, _log = env
    marker = build_stop.lock_marker_path(cfg)

    def fail(src: os.PathLike[str], dst: os.PathLike[str]) -> None:
        raise OSError(1, "no links")

    monkeypatch.setattr(os, "link", fail)
    with pytest.raises(OSError, match="no links"):
        kas_lock._create_marker_exclusive(marker, "this-host")
    assert not marker.exists()
    assert list(build.iterdir()) == []


def test_foreign_marker_still_refused(env: tuple[BuildConfig, Path, RunLogger]) -> None:
    cfg, _build, log = env
    build_stop.lock_marker_path(cfg).write_text("peer")
    with pytest.raises(LockHeldByPeerError) as ei, lock_owner_marker(cfg, log):
        pass
    assert not isinstance(ei.value, LockUnattributableError)
    assert ei.value.host == "peer"


def test_own_marker_is_replaced(env: tuple[BuildConfig, Path, RunLogger], monkeypatch: pytest.MonkeyPatch) -> None:
    cfg, build, log = env
    _on_nfs(monkeypatch, True)
    (build / "bitbake.lock").write_text("1\n")
    marker = build_stop.lock_marker_path(cfg)
    marker.write_text("this-host")
    with lock_owner_marker(cfg, log):
        assert marker.read_text() == "this-host"


def test_unreadable_marker_on_nfs_with_lock_refuses(
    env: tuple[BuildConfig, Path, RunLogger], monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, build, log = env
    _on_nfs(monkeypatch, True)
    (build / "bitbake.lock").write_text("1\n")
    marker = build_stop.lock_marker_path(cfg)
    marker.write_text("peer")
    _break_read(monkeypatch, marker)
    entered = False
    with pytest.raises(LockUnattributableError) as ei, lock_owner_marker(cfg, log):
        entered = True
    assert not entered
    assert isinstance(ei.value, LockHeldByPeerError)
    assert "could not be attributed" in str(ei.value)
    assert str(marker) in str(ei.value)
    assert marker.read_bytes() == b"peer"  # not overwritten


def test_empty_marker_on_unverifiable_fs_with_lock_refuses(
    env: tuple[BuildConfig, Path, RunLogger], monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, build, log = env
    _on_nfs(monkeypatch, None)
    (build / "bitbake.lock").write_text("1\n")
    marker = build_stop.lock_marker_path(cfg)
    marker.write_text("")
    with pytest.raises(LockUnattributableError), lock_owner_marker(cfg, log):
        pass
    assert marker.read_text() == ""


def test_unreadable_marker_on_local_fs_is_replaced(
    env: tuple[BuildConfig, Path, RunLogger], monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, build, log = env
    _on_nfs(monkeypatch, False)
    (build / "bitbake.lock").write_text("1\n")
    marker = build_stop.lock_marker_path(cfg)
    marker.write_text("peer")
    _break_read(monkeypatch, marker)
    with lock_owner_marker(cfg, log):
        pass
    monkeypatch.undo()
    assert marker.read_text() == "this-host"


def test_unreadable_marker_on_nfs_without_lock_is_replaced(
    env: tuple[BuildConfig, Path, RunLogger], monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, _build, log = env
    _on_nfs(monkeypatch, True)
    marker = build_stop.lock_marker_path(cfg)
    marker.write_text("")
    with lock_owner_marker(cfg, log):
        assert marker.read_text() == "this-host"


def test_garbled_marker_on_nfs_with_lock_is_replaced(
    env: tuple[BuildConfig, Path, RunLogger], monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, build, log = env
    _on_nfs(monkeypatch, True)
    (build / "bitbake.lock").write_text("1\n")
    marker = build_stop.lock_marker_path(cfg)
    marker.write_text("a\nb\x00")
    with lock_owner_marker(cfg, log):
        assert marker.read_text() == "this-host"


def test_marker_readable_on_reread_is_honoured(
    env: tuple[BuildConfig, Path, RunLogger], monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, build, log = env
    _on_nfs(monkeypatch, True)
    (build / "bitbake.lock").write_text("1\n")
    marker = build_stop.lock_marker_path(cfg)
    marker.write_text("")

    def fill(_s: float) -> None:
        marker.write_text("peer")

    monkeypatch.setattr(kas_lock.time, "sleep", fill)
    with pytest.raises(LockHeldByPeerError) as ei, lock_owner_marker(cfg, log):
        pass
    assert ei.value.host == "peer"
    assert not isinstance(ei.value, LockUnattributableError)


@pytest.mark.parametrize(
    ("content", "state"),
    [("", "empty"), ("  \n", "empty"), ("a\nb", "garbled"), ("x\x00y", "garbled"), ("host\n", "owner")],
)
def test_read_marker_state(env: tuple[BuildConfig, Path, RunLogger], content: str, state: str) -> None:
    cfg, _build, _log = env
    assert build_stop.read_marker_state(cfg) == "absent"
    build_stop.lock_marker_path(cfg).write_text(content)
    assert build_stop.read_marker_state(cfg) == state


def test_read_marker_state_unreadable(
    env: tuple[BuildConfig, Path, RunLogger], monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, _build, _log = env
    marker = build_stop.lock_marker_path(cfg)
    marker.write_text("peer")
    _break_read(monkeypatch, marker)
    assert build_stop.read_marker_state(cfg) == "unreadable"
    assert build_stop.read_marker_owner(cfg) is None


@pytest.mark.parametrize("umask", [0o022, 0o077, 0o002])
def test_created_marker_mode_matches_open_x(tmp_path: Path, umask: int) -> None:
    old = os.umask(umask)
    try:
        ref = tmp_path / "ref"
        with open(ref, "x", encoding="utf-8") as fh:
            fh.write("h")
        marker = tmp_path / "marker"
        assert kas_lock._create_marker_exclusive(marker, "h") is True
        assert marker.stat().st_mode & 0o777 == ref.stat().st_mode & 0o777
    finally:
        os.umask(old)


def test_replaced_marker_mode_honours_umask(env: tuple[BuildConfig, Path, RunLogger]) -> None:
    cfg, _build, log = env
    marker = build_stop.lock_marker_path(cfg)
    marker.write_text("a\nb")
    old = os.umask(0o022)
    try:
        with lock_owner_marker(cfg, log):
            assert marker.stat().st_mode & 0o777 == 0o644
    finally:
        os.umask(old)


def test_unattributable_marker_at_capture_site_logs_manual_clear(
    env: tuple[BuildConfig, Path, RunLogger], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from bakar.steps import kas_build

    cfg, build, log = env
    _on_nfs(monkeypatch, True)
    (build / "bitbake.lock").write_text("1\n")
    marker = build_stop.lock_marker_path(cfg)
    marker.write_text("peer")
    _break_read(monkeypatch, marker)
    monkeypatch.setattr(kas_build, "cache_mount_refusal", lambda _cfg, **_kw: None)
    monkeypatch.setattr(kas_build, "clear_stale_bitbake_locks", lambda _cfg: build_stop.LockClearOutcome(removed=[]))
    failures: list[str] = []
    monkeypatch.setattr(log, "step_fail", lambda _step, reason="", **_kw: failures.append(reason))
    kas_yaml = cfg.bsp_root / "kas.yml"
    kas_yaml.write_text("header:\n  version: 14\n")
    ctx = kas_build.KasBuildContext(cfg=cfg, log=log, kas_yaml=kas_yaml, overlay_source=kas_yaml)

    with log:
        rc = kas_build.run_shell_capture(ctx, "true", kas_build.ShellCapture(stdout_path=tmp_path / "out.log"))

    assert rc == 1
    assert len(failures) == 1
    assert str(marker) in failures[0]
    assert "delete" in failures[0]
    assert "manually" in failures[0]
    assert "unknown (marker unreadable)" not in failures[0]
