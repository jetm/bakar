"""Tests for the one-step ownership marker and the unreadable-marker refusal."""

from __future__ import annotations

import errno
import os
import shutil
import socket
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from bakar import build_stop, observability
from bakar.config import BuildConfig
from bakar.observability import RunLogger
from bakar.steps import kas_lock
from bakar.steps.kas_lock import (
    LockHeldByPeerError,
    LockMarkerFileError,
    LockMarkerUnsupportedError,
    LockUnattributableError,
    lock_owner_marker,
    peer_lock_reason,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager


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
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(socket, "gethostname", lambda: "this-host")
    monkeypatch.setattr(kas_lock.time, "sleep", lambda _s: None)
    return cfg, build, RunLogger(runs_dir=cfg.runs_dir)


def _on_nfs(monkeypatch: pytest.MonkeyPatch, value: bool | None) -> None:
    monkeypatch.setattr("bakar.mounts.is_path_on_nfs", lambda _p: value)


def _break_read(monkeypatch: pytest.MonkeyPatch, marker: Path) -> None:
    real_text = Path.read_text
    real_bytes = Path.read_bytes

    def fake_text(self: Path, *a: object, **k: object) -> str:
        if self == marker:
            raise PermissionError("denied")
        return real_text(self, *a, **k)  # type: ignore[arg-type]

    def fake_bytes(self: Path) -> bytes:
        if self == marker:
            raise PermissionError("denied")
        return real_bytes(self)

    monkeypatch.setattr(Path, "read_text", fake_text)
    monkeypatch.setattr(Path, "read_bytes", fake_bytes)


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


def test_link_error_with_marker_naming_this_host_is_success(
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


@pytest.mark.parametrize("err", [errno.EPERM, errno.EIO])
def test_link_failure_without_host_naming_marker_raises_unsupported(
    env: tuple[BuildConfig, Path, RunLogger], monkeypatch: pytest.MonkeyPatch, err: int
) -> None:
    """A link failure that did not publish the marker is a clear refusal, never a raw OSError."""
    cfg, build, _log = env
    marker = build_stop.lock_marker_path(cfg)

    def fail(src: os.PathLike[str], dst: os.PathLike[str]) -> None:
        raise OSError(err, "no links")

    monkeypatch.setattr(os, "link", fail)
    with pytest.raises(LockMarkerUnsupportedError) as ei:
        kas_lock._create_marker_exclusive(marker, "this-host")
    assert isinstance(ei.value, LockHeldByPeerError)
    assert os.strerror(err) in str(ei.value)
    assert str(marker) in str(ei.value)
    assert not marker.exists()
    assert list(build.iterdir()) == []


def test_link_lost_reply_with_other_host_marker_is_refused(
    env: tuple[BuildConfig, Path, RunLogger], monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, _build, _log = env
    marker = build_stop.lock_marker_path(cfg)
    marker.write_text("peer")

    def fail(src: os.PathLike[str], dst: os.PathLike[str]) -> None:
        raise OSError(errno.EIO, "reply lost")

    monkeypatch.setattr(os, "link", fail)
    with pytest.raises(LockMarkerUnsupportedError):
        kas_lock._create_marker_exclusive(marker, "this-host")
    assert marker.read_text() == "peer"


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


def test_exit_leaves_marker_a_peer_overwrote_during_the_body(env: tuple[BuildConfig, Path, RunLogger]) -> None:
    cfg, _build, log = env
    marker = build_stop.lock_marker_path(cfg)
    with lock_owner_marker(cfg, log):
        marker.write_text("peer")
    assert marker.read_text() == "peer"


def test_exit_leaves_unreadable_marker(
    env: tuple[BuildConfig, Path, RunLogger], monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, _build, log = env
    marker = build_stop.lock_marker_path(cfg)
    with lock_owner_marker(cfg, log):
        _break_read(monkeypatch, marker)
    assert marker.exists()


def test_exit_removes_own_marker_when_lock_absent(env: tuple[BuildConfig, Path, RunLogger]) -> None:
    cfg, build, log = env
    marker = build_stop.lock_marker_path(cfg)
    with lock_owner_marker(cfg, log):
        assert marker.read_text() == "this-host"
    assert not marker.exists()
    assert not (build / "bitbake.lock").exists()


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
    with marker.open("rb") as fh:
        assert fh.read() == b"peer"  # not overwritten


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


def test_unreadable_marker_on_nfs_without_lock_is_refused(
    env: tuple[BuildConfig, Path, RunLogger], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absence of bitbake.lock on NFS proves nothing (negative-lookup caching), so it must not license an overwrite."""
    cfg, _build, log = env
    _on_nfs(monkeypatch, True)
    marker = build_stop.lock_marker_path(cfg)
    marker.write_text("")
    entered = False
    with pytest.raises(LockUnattributableError), lock_owner_marker(cfg, log):
        entered = True
    assert not entered
    assert marker.read_bytes() == b""


def test_broken_read_marker_on_nfs_without_lock_is_refused(
    env: tuple[BuildConfig, Path, RunLogger], monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, _build, log = env
    _on_nfs(monkeypatch, True)
    marker = build_stop.lock_marker_path(cfg)
    marker.write_text("peer")
    _break_read(monkeypatch, marker)
    with pytest.raises(LockUnattributableError), lock_owner_marker(cfg, log):
        pass
    monkeypatch.undo()
    assert marker.read_bytes() == b"peer"


def test_empty_marker_on_unverifiable_fs_without_lock_is_refused(
    env: tuple[BuildConfig, Path, RunLogger], monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, _build, log = env
    _on_nfs(monkeypatch, None)
    marker = build_stop.lock_marker_path(cfg)
    marker.write_text("")
    with pytest.raises(LockUnattributableError), lock_owner_marker(cfg, log):
        pass
    assert marker.read_bytes() == b""


def test_peer_write_between_reread_and_guard_is_honoured(
    env: tuple[BuildConfig, Path, RunLogger], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two classifying reads see an empty marker; the guard's own read sees the peer's name."""
    cfg, _build, log = env
    _on_nfs(monkeypatch, True)
    marker = build_stop.lock_marker_path(cfg)
    marker.write_text("peer")
    real = build_stop.read_marker
    calls: list[int] = []

    def late_visibility(c: BuildConfig) -> tuple[str, str | None]:
        calls.append(1)
        if len(calls) <= 2:
            return "empty", None
        return real(c)

    monkeypatch.setattr(build_stop, "read_marker", late_visibility)
    entered = False
    with pytest.raises(LockHeldByPeerError) as ei, lock_owner_marker(cfg, log):
        entered = True
    assert not entered
    assert not isinstance(ei.value, LockUnattributableError)
    assert ei.value.host == "peer"
    assert marker.read_bytes() == b"peer"


def test_garbled_marker_on_confirmed_local_fs_is_replaced(
    env: tuple[BuildConfig, Path, RunLogger], monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, build, log = env
    _on_nfs(monkeypatch, False)
    (build / "bitbake.lock").write_text("1\n")
    marker = build_stop.lock_marker_path(cfg)
    marker.write_text("a\nb\x00")
    with lock_owner_marker(cfg, log):
        assert marker.read_text() == "this-host"


def test_marker_vanished_after_failed_create_is_recreated_exclusively(
    env: tuple[BuildConfig, Path, RunLogger], monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, _build, log = env
    marker = build_stop.lock_marker_path(cfg)
    marker.write_text("peer")
    real = build_stop.read_marker
    reads: list[int] = []

    def vanish(c: BuildConfig) -> tuple[str, str | None]:
        reads.append(1)
        marker.unlink(missing_ok=True)
        return real(c)

    monkeypatch.setattr(build_stop, "read_marker", vanish)
    with lock_owner_marker(cfg, log):
        assert marker.read_text() == "this-host"
    assert reads == [1]


def test_marker_absent_but_never_creatable_retries_then_refuses(
    env: tuple[BuildConfig, Path, RunLogger], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read that keeps saying absent while the create keeps losing never falls through to a replace."""
    cfg, _build, log = env
    marker = build_stop.lock_marker_path(cfg)
    marker.write_text("peer")
    monkeypatch.setattr(build_stop, "read_marker", lambda _c: ("absent", None))
    creates: list[int] = []
    real_create = kas_lock._create_marker_exclusive

    def counting(m: Path, h: str) -> bool:
        creates.append(1)
        return real_create(m, h)

    monkeypatch.setattr(kas_lock, "_create_marker_exclusive", counting)
    with pytest.raises(LockUnattributableError), lock_owner_marker(cfg, log):
        pass
    assert len(creates) == 1 + kas_lock._MARKER_CREATE_ATTEMPTS
    assert marker.read_bytes() == b"peer"


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


@pytest.mark.parametrize("err", [errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP])
def test_create_marker_refuses_clearly_when_hard_links_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, err: int
) -> None:
    """No fallback exposes an empty marker: the refusal names hard links and leaves nothing behind."""

    def no_link(_src: object, _dst: object) -> None:
        raise OSError(err, os.strerror(err))

    monkeypatch.setattr(kas_lock.os, "link", no_link)
    marker = tmp_path / "bitbake.lock.owner"
    with pytest.raises(LockMarkerUnsupportedError) as exc:
        kas_lock._create_marker_exclusive(marker, "this-host")
    assert isinstance(exc.value, LockHeldByPeerError)
    assert "does not support hard links" in str(exc.value)
    assert str(marker) in str(exc.value)
    assert not marker.exists()
    assert list(tmp_path.iterdir()) == []


def test_create_marker_other_link_errors_refuse_with_errno_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def full(_src: object, _dst: object) -> None:
        raise OSError(errno.ENOSPC, "no space")

    monkeypatch.setattr(kas_lock.os, "link", full)
    marker = tmp_path / "bitbake.lock.owner"
    with pytest.raises(LockMarkerUnsupportedError) as exc:
        kas_lock._create_marker_exclusive(marker, "this-host")
    assert os.strerror(errno.ENOSPC) in str(exc.value)
    assert not marker.exists()
    assert list(tmp_path.iterdir()) == []


def test_lock_owner_marker_on_hardlink_less_fs_never_enters_or_leaves_a_marker(
    env: tuple[BuildConfig, Path, RunLogger], monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, build, log = env

    def no_link(_src: object, _dst: object) -> None:
        raise OSError(errno.EOPNOTSUPP, os.strerror(errno.EOPNOTSUPP))

    monkeypatch.setattr(kas_lock.os, "link", no_link)
    entered = False
    with pytest.raises(LockHeldByPeerError), lock_owner_marker(cfg, log):
        entered = True
    assert not entered
    assert list(build.iterdir()) == []


def test_marker_unsupported_error_is_printed_without_traceback_reason() -> None:
    marker = Path("/b/\x1b]0;x\x07.bakar-lock-host")
    reason = peer_lock_reason(LockMarkerUnsupportedError(marker, OSError(errno.EPERM, "x")))
    assert "\x1b" not in reason
    assert "hard links" in reason


def test_peer_lock_reason_for_a_plain_peer_names_marker_and_manual_clear() -> None:
    marker = Path("/b/.bakar-lock-host")
    reason = peer_lock_reason(LockHeldByPeerError("node-b", marker=marker))
    assert "node-b" in reason
    assert str(marker) in reason
    assert "by hand" in reason
    # Without a marker the reason is the original, hint-free wording.
    bare = peer_lock_reason(LockHeldByPeerError("node-b"))
    assert "by hand" not in bare
    assert reason.startswith(bare)


def _lost_reply_link(monkeypatch: pytest.MonkeyPatch) -> None:
    real_link = os.link

    def lost_reply(src: os.PathLike[str], dst: os.PathLike[str]) -> None:
        real_link(src, dst)
        raise OSError(errno.EIO, "reply lost")

    monkeypatch.setattr(os, "link", lost_reply)


def _failing_unlink_of(monkeypatch: pytest.MonkeyPatch, prefix: str) -> None:
    real = Path.unlink

    def fake(self: Path, *a: object, **k: object) -> None:
        if self.name.startswith(prefix):
            raise OSError(errno.EIO, "unlink failed")
        real(self, *a, **k)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "unlink", fake)


def test_link_lost_reply_with_unreadable_marker_counts_as_created(
    env: tuple[BuildConfig, Path, RunLogger], monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, _build, log = env
    marker = build_stop.lock_marker_path(cfg)
    _lost_reply_link(monkeypatch)
    _break_read(monkeypatch, marker)
    entered = False
    with lock_owner_marker(cfg, log):
        entered = True
    assert entered


def test_link_lost_reply_outcome_unprovable_is_refused_with_marker_named(
    env: tuple[BuildConfig, Path, RunLogger], monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, _build, log = env
    marker = build_stop.lock_marker_path(cfg)
    _lost_reply_link(monkeypatch)
    _break_read(monkeypatch, marker)
    real_stat = os.stat

    def stat(path: object, *a: object, **k: object) -> os.stat_result:
        if Path(str(path)).name.startswith(f".{marker.name}."):
            raise OSError(errno.EIO, "stat failed")
        return real_stat(path, *a, **k)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "stat", stat)
    entered = False
    with pytest.raises(LockHeldByPeerError) as ei, lock_owner_marker(cfg, log):
        entered = True
    assert not entered
    assert isinstance(ei.value, kas_lock.LockMarkerOutcomeUnknownError)
    assert str(marker) in str(ei.value)
    assert "by hand" in str(ei.value)
    assert "does not name this host" not in str(ei.value)


def test_temp_unlink_failure_never_replaces_the_result(
    env: tuple[BuildConfig, Path, RunLogger], monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, _build, _log = env
    marker = build_stop.lock_marker_path(cfg)
    _failing_unlink_of(monkeypatch, f".{marker.name}.")
    assert kas_lock._create_marker_exclusive(marker, "this-host") is True

    marker.unlink()
    monkeypatch.undo()
    monkeypatch.setattr(socket, "gethostname", lambda: "this-host")
    _failing_unlink_of(monkeypatch, f".{marker.name}.")

    def no_link(_src: object, _dst: object) -> None:
        raise OSError(errno.EPERM, "no links")

    monkeypatch.setattr(os, "link", no_link)
    with pytest.raises(LockMarkerUnsupportedError):
        kas_lock._create_marker_exclusive(marker, "this-host")


def test_replace_lost_reply_is_refused_as_unknown_outcome(
    env: tuple[BuildConfig, Path, RunLogger], monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, _build, _log = env
    marker = build_stop.lock_marker_path(cfg)
    marker.write_text("bad host!\n\x01")

    def fail(_src: object, _dst: object) -> None:
        raise OSError(errno.EIO, "reply lost")

    monkeypatch.setattr(os, "replace", fail)
    _failing_unlink_of(monkeypatch, f".{marker.name}.")
    with pytest.raises(kas_lock.LockMarkerOutcomeUnknownError) as ei:
        kas_lock._replace_marker(marker, "this-host")
    assert str(marker) in str(ei.value)
    assert isinstance(ei.value, LockHeldByPeerError)


def test_exit_unlink_failure_is_logged_and_swallowed(
    env: tuple[BuildConfig, Path, RunLogger], monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, _build, log = env
    marker = build_stop.lock_marker_path(cfg)
    warnings: list[str] = []
    monkeypatch.setattr(log, "warn", lambda msg, **_f: warnings.append(msg))
    with lock_owner_marker(cfg, log):
        _failing_unlink_of(monkeypatch, marker.name)
    assert len(warnings) == 1
    assert str(marker) in warnings[0]


def test_link_failure_proven_not_landed_names_marker_and_manual_clear(
    env: tuple[BuildConfig, Path, RunLogger], monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, _build, _log = env
    marker = build_stop.lock_marker_path(cfg)

    def fail(_src: object, _dst: object) -> None:
        raise OSError(errno.EIO, "reply lost")

    monkeypatch.setattr(os, "link", fail)
    with pytest.raises(LockMarkerUnsupportedError) as ei:
        kas_lock._create_marker_exclusive(marker, "this-host")
    assert str(marker) in str(ei.value)
    assert "by hand" in str(ei.value)


def test_peer_held_refusal_carries_clear_hint(
    env: tuple[BuildConfig, Path, RunLogger], monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, _build, _log = env
    marker = build_stop.lock_marker_path(cfg)
    marker.write_text("pc1")
    monkeypatch.setattr(socket, "gethostname", lambda: "pc2")
    _on_nfs(monkeypatch, True)

    outcome = kas_lock.clear_stale_bitbake_locks(cfg)

    assert outcome.refusal is not None
    assert outcome.refusal.reason == "peer-held"
    assert outcome.refusal.marker == marker
    message = kas_lock._lock_refusal_message(outcome.refusal)
    assert str(marker) in message
    assert "by hand" in message


def test_lock_mutation_guard_refusal_carries_clear_hint(
    env: tuple[BuildConfig, Path, RunLogger], monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, build, _log = env
    marker = build_stop.lock_marker_path(cfg)
    _on_nfs(monkeypatch, True)

    # No marker, lock present, shared filesystem: unattributable.
    (build / "bitbake.lock").write_text("123\n")
    refusal = build_stop.lock_mutation_guard(cfg)
    assert refusal is not None
    assert refusal.reason == "unattributable"
    assert refusal.marker == marker

    # No marker, no lock, shared filesystem: shared-inaction.
    (build / "bitbake.lock").unlink()
    refusal = build_stop.lock_mutation_guard(cfg)
    assert refusal is not None
    assert refusal.reason == "shared-inaction"
    assert refusal.marker == marker

    # Foreign marker: peer-held.
    marker.write_text("pc1")
    refusal = build_stop.lock_mutation_guard(cfg)
    assert refusal is not None
    assert refusal.reason == "peer-held"
    assert refusal.marker == marker


def test_clear_hint_text_names_marker_and_lock(tmp_path: Path) -> None:
    marker = tmp_path / "build" / ".bakar-lock-host"
    hint = build_stop.manual_clear_hint(marker)
    assert str(marker) in hint
    assert "bitbake.lock" in hint
    assert "by hand" in hint
    assert hint.endswith(".")
    assert "\n" not in hint
    assert build_stop.LockRefusal(reason="peer-held").marker is None


def test_unattributable_clear_refusal_carries_the_marker(
    env: tuple[BuildConfig, Path, RunLogger], monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, build, _log = env
    marker = build_stop.lock_marker_path(cfg)
    (build / "bitbake.lock").write_text("123\n")
    _on_nfs(monkeypatch, True)

    outcome = kas_lock.clear_stale_bitbake_locks(cfg)

    assert outcome.refusal is not None
    assert outcome.refusal.reason == "unattributable"
    assert outcome.refusal.marker == marker
    message = kas_lock._lock_refusal_message(outcome.refusal)
    assert str(marker) in message
    assert "by hand" in message


# ---------------------------------------------------------------------------
# local_build_holder: real fake bitbake-server processes, the process collector
# runs for real against /proc in every test below.
# ---------------------------------------------------------------------------


def _tree_snapshot(root: Path) -> dict[str, tuple[int, int, bytes | None]]:
    """Every path under ``root`` with its mtime, size and (for files) contents."""
    return {
        str(p): (p.stat().st_mtime_ns, p.stat().st_size, p.read_bytes() if p.is_file() else None)
        for p in sorted(root.rglob("*"))
    }


def _run_dir(cfg: BuildConfig, *, started: datetime | None = None, pid: int = 4242) -> Path:
    stamp = (started or datetime.now(UTC)).strftime(observability.RUN_ID_TS_FORMAT)
    run_dir = cfg.runs_dir / f"{stamp}-{pid}"
    run_dir.mkdir(parents=True)
    return run_dir


def _dead_pid(live_child: Callable[..., AbstractContextManager[subprocess.Popen[bytes]]]) -> int:
    with live_child() as child:
        pass  # exiting the context terminates and reaps it
    return child.pid


def _fake_runtime(tmp_path: Path, *, container_alive: bool) -> Path:
    """A runtime whose ``ps`` lists the ``bakar.run_id=x`` container while it is alive.

    The output is ``discover_running_containers``' ``ID<TAB>run id`` format;
    each call appends one line to ``calls.log`` beside the script.
    """
    script = tmp_path / "docker"
    calls = tmp_path / "calls.log"
    listing = "printf 'cid0123\\tx\\n'\n" if container_alive else ""
    script.write_text(f"#!/bin/sh\necho \"$*\" >> '{calls}'\n{listing}exit 0\n")
    script.chmod(0o755)
    return Path("docker")  # recorded by name; the env fixture puts tmp_path on PATH


def _failing_runtime(tmp_path: Path) -> Path:
    script = tmp_path / "docker"
    script.write_text("#!/bin/sh\necho 'Cannot connect to the daemon' >&2\nexit 1\n")
    script.chmod(0o755)
    return Path("docker")


def test_local_build_holder_reports_a_live_pid_with_activity(
    env: tuple[BuildConfig, Path, RunLogger],
    bitbake_server: Callable[..., AbstractContextManager[subprocess.Popen[bytes]]],
) -> None:
    cfg, build, _log = env
    with bitbake_server(build, children="worker") as server:
        (build / "bitbake.lock").write_text(f"{server.pid}\n")
        assert kas_lock.local_build_holder(cfg) == server.pid


def test_local_build_holder_reports_a_server_with_no_child_while_its_launch_record_is_live(
    env: tuple[BuildConfig, Path, RunLogger],
    bitbake_server: Callable[..., AbstractContextManager[subprocess.Popen[bytes]]],
    live_child: Callable[..., AbstractContextManager[subprocess.Popen[bytes]]],
) -> None:
    """Task initialisation runs before bitbake starts a worker: no child, still a build."""
    cfg, build, _log = env
    with bitbake_server(build) as server, live_child("kas-container") as wrapper:
        (build / "bitbake.lock").write_text(f"{server.pid}\n")
        build_stop.write_launch_record(_run_dir(cfg), pgid=wrapper.pid, mode="host")
        assert kas_lock.local_build_holder(cfg) == server.pid


def test_local_build_holder_counts_an_attached_client_in_its_own_session(
    env: tuple[BuildConfig, Path, RunLogger],
    bitbake_server: Callable[..., AbstractContextManager[subprocess.Popen[bytes]]],
    live_child: Callable[..., AbstractContextManager[subprocess.Popen[bytes]]],
) -> None:
    """The client is no child of the server; the recorded group seeds the scan."""
    cfg, build, _log = env
    with bitbake_server(build) as server, live_child("bitbake", "core-image-minimal") as client:
        assert os.getpgid(client.pid) != os.getpgid(server.pid)
        (build / "bitbake.lock").write_text(f"{server.pid}\n")
        build_stop.write_pid(_run_dir(cfg), client.pid)
        assert kas_lock.local_build_holder(cfg) == server.pid


def test_local_build_holder_reports_a_container_build_with_a_live_launch_record(
    env: tuple[BuildConfig, Path, RunLogger],
    tmp_path: Path,
    bitbake_server: Callable[..., AbstractContextManager[subprocess.Popen[bytes]]],
    live_child: Callable[..., AbstractContextManager[subprocess.Popen[bytes]]],
) -> None:
    """Container pid in the lock and /work/... argv: the launch record decides."""
    cfg, build, _log = env
    container_pid = _dead_pid(live_child)
    wrapper_pid = _dead_pid(live_child)  # the kas-container wrapper exited; the container lives
    run_dir = _run_dir(cfg)
    with bitbake_server("/work/build", children="worker"):
        (build / "bitbake.lock").write_text(f"{container_pid}\n")
        build_stop.write_launch_record(
            run_dir,
            pgid=wrapper_pid,
            mode="container",
            runtime=str(_fake_runtime(tmp_path, container_alive=True)),
            container_label="bakar.run_id=x",
        )
        assert kas_lock.local_build_holder(cfg) == wrapper_pid

        build_stop.write_launch_record(
            run_dir,
            pgid=wrapper_pid,
            mode="container",
            runtime=str(_fake_runtime(tmp_path, container_alive=False)),
            container_label="bakar.run_id=x",
        )
        assert kas_lock.local_build_holder(cfg) is None


def test_local_build_holder_reports_an_empty_lock_with_a_live_launch_record(
    env: tuple[BuildConfig, Path, RunLogger],
    live_child: Callable[..., AbstractContextManager[subprocess.Popen[bytes]]],
) -> None:
    cfg, build, _log = env
    (build / "bitbake.lock").write_text("")
    with live_child("kas-container") as wrapper:
        build_stop.write_launch_record(
            _run_dir(cfg, started=datetime(2020, 1, 1, tzinfo=UTC)), pgid=wrapper.pid, mode="host"
        )
        assert kas_lock.local_build_holder(cfg) == wrapper.pid


def test_local_build_holder_reads_an_empty_lock_from_a_recent_run_on_this_host_as_a_holder(
    env: tuple[BuildConfig, Path, RunLogger],
    live_child: Callable[..., AbstractContextManager[subprocess.Popen[bytes]]],
) -> None:
    cfg, build, _log = env
    (build / "bitbake.lock").write_text("")
    build_stop.lock_marker_path(cfg).write_text("this-host\n")
    with live_child() as starter:
        _run_dir(cfg, pid=starter.pid)
        assert kas_lock.local_build_holder(cfg) == starter.pid


def test_holder_empty_lock_dead_pid_is_none(
    env: tuple[BuildConfig, Path, RunLogger],
    live_child: Callable[..., AbstractContextManager[subprocess.Popen[bytes]]],
) -> None:
    """A recent run whose pid has exited is not a server still writing the lock."""
    cfg, build, _log = env
    (build / "bitbake.lock").write_text("")
    build_stop.lock_marker_path(cfg).write_text("this-host\n")
    _run_dir(cfg, pid=_dead_pid(live_child))

    assert kas_lock.local_build_holder(cfg) is None


def test_holder_empty_lock_skips_own_run(
    env: tuple[BuildConfig, Path, RunLogger],
    live_child: Callable[..., AbstractContextManager[subprocess.Popen[bytes]]],
) -> None:
    """stress-parse's own run directory is the newest: it must not name the caller's own pid."""
    cfg, build, _log = env
    (build / "bitbake.lock").write_text("")
    build_stop.lock_marker_path(cfg).write_text("this-host\n")
    _run_dir(cfg, pid=os.getpid())
    assert kas_lock.local_build_holder(cfg) is None

    with live_child() as other:
        _run_dir(cfg, started=datetime.now(UTC) - timedelta(seconds=30), pid=other.pid)
        assert kas_lock.local_build_holder(cfg) == other.pid


def test_local_build_holder_ignores_an_empty_lock_without_a_recent_run_or_own_marker(
    env: tuple[BuildConfig, Path, RunLogger],
) -> None:
    cfg, build, _log = env
    (build / "bitbake.lock").write_text("")
    assert kas_lock.local_build_holder(cfg) is None  # no run, no marker

    recent = _run_dir(cfg, pid=4242)
    assert kas_lock.local_build_holder(cfg) is None  # recent run, but no marker naming this host

    build_stop.lock_marker_path(cfg).write_text("this-host\n")
    old = datetime.now(UTC) - timedelta(seconds=kas_lock._EMPTY_LOCK_RECENT_RUN_S + 60)
    recent.rename(cfg.runs_dir / f"{old.strftime(observability.RUN_ID_TS_FORMAT)}-4242")
    assert kas_lock.local_build_holder(cfg) is None  # marker names this host, the run is old


def test_local_build_holder_ignores_an_idle_cooker(
    env: tuple[BuildConfig, Path, RunLogger],
    bitbake_server: Callable[..., AbstractContextManager[subprocess.Popen[bytes]]],
) -> None:
    """A warm idle cooker with no launch record is not a holder (the user's ruling)."""
    cfg, build, _log = env
    build_stop.lock_marker_path(cfg).write_text("this-host\n")
    with bitbake_server(build) as server:
        (build / "bitbake.lock").write_text(f"{server.pid}\n")
        assert kas_lock.local_build_holder(cfg) is None


def test_local_build_holder_ignores_an_idle_cooker_with_its_autostarted_hashserv(
    env: tuple[BuildConfig, Path, RunLogger],
    bitbake_server: Callable[..., AbstractContextManager[subprocess.Popen[bytes]]],
) -> None:
    """BB_HASHSERVE=auto keeps a forked hashserv child beside an idle cooker."""
    cfg, build, _log = env
    with bitbake_server(build, children="service") as server:
        (build / "bitbake.lock").write_text(f"{server.pid}\n")
        assert kas_lock.local_build_holder(cfg) is None


def test_local_build_holder_ignores_an_idle_cooker_started_through_the_resolved_path(
    env: tuple[BuildConfig, Path, RunLogger],
    tmp_path: Path,
    bitbake_server: Callable[..., AbstractContextManager[subprocess.Popen[bytes]]],
) -> None:
    """A symlinked workspace puts the resolved path in the server's argv."""
    _cfg_real, build, _log = env
    link = tmp_path / "linked-ws"
    link.symlink_to(tmp_path, target_is_directory=True)
    cfg = _cfg(link)
    with bitbake_server(build.resolve()) as server:
        (build / "bitbake.lock").write_text(f"{server.pid}\n")
        assert kas_lock.local_build_holder(cfg) is None
    with bitbake_server(build.resolve(), children="worker") as server:
        (build / "bitbake.lock").write_text(f"{server.pid}\n")
        assert kas_lock.local_build_holder(cfg) == server.pid


def test_local_build_holder_ignores_a_dead_or_reused_pid(
    env: tuple[BuildConfig, Path, RunLogger],
    live_child: Callable[..., AbstractContextManager[subprocess.Popen[bytes]]],
) -> None:
    cfg, build, _log = env
    lock = build / "bitbake.lock"

    lock.write_text(f"{_dead_pid(live_child)}\n")
    assert kas_lock.local_build_holder(cfg) is None

    with live_child() as reused:  # live pid, argv does not mention bitbake
        lock.write_text(f"{reused.pid}\n")
        assert kas_lock.local_build_holder(cfg) is None


def test_local_build_holder_never_mutates_the_build_directory(
    env: tuple[BuildConfig, Path, RunLogger],
    tmp_path: Path,
    bitbake_server: Callable[..., AbstractContextManager[subprocess.Popen[bytes]]],
    live_child: Callable[..., AbstractContextManager[subprocess.Popen[bytes]]],
) -> None:
    cfg, build, _log = env
    (build / "bitbake.sock").write_text("")
    build_stop.lock_marker_path(cfg).write_text("this-host\n")
    with bitbake_server(build, children="worker") as busy, bitbake_server(build) as idle:
        (build / "bitbake.lock").write_text(f"{busy.pid}\n")
        before = _tree_snapshot(build)
        assert kas_lock.local_build_holder(cfg) == busy.pid
        (build / "bitbake.lock").write_text(f"{idle.pid}\n")
        before_idle = _tree_snapshot(build)
        # Both servers name this build dir, so the scan sees busy's worker too.
        assert kas_lock.local_build_holder(cfg) == idle.pid
        assert _tree_snapshot(build) == before_idle
    assert set(before) == set(_tree_snapshot(build))
    with live_child("kas-container") as wrapper:
        run_dir = _run_dir(cfg)
        (build / "bitbake.lock").write_text("")
        build_stop.write_launch_record(
            run_dir,
            pgid=wrapper.pid,
            mode="container",
            runtime=str(_fake_runtime(tmp_path, container_alive=True)),
            container_label="bakar.run_id=x",
        )
        before_record = _tree_snapshot(build)
        assert kas_lock.local_build_holder(cfg) == wrapper.pid
        assert _tree_snapshot(build) == before_record

    # The None verdicts are held to the same no-mutation rule. The container
    # record above stays live while its fake runtime answers, so drop it first.
    shutil.rmtree(run_dir)
    lock = build / "bitbake.lock"
    for write_lock in (
        lambda: lock.write_text(""),  # empty lock, the starter is gone
        lambda: lock.write_text(f"{_dead_pid(live_child)}\n"),  # dead pid
        lambda: lock.unlink(missing_ok=True),  # no lock at all
    ):
        write_lock()
        before_none = _tree_snapshot(build)
        assert kas_lock.local_build_holder(cfg) is None
        assert _tree_snapshot(build) == before_none


def test_lock_pid_liveness_treats_a_cmdline_read_race_as_live(
    env: tuple[BuildConfig, Path, RunLogger],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The process exiting between ``os.kill`` and the cmdline read must not escape as a traceback."""
    cfg, build, _log = env
    pid = os.getpid()
    cmdline_path = Path(f"/proc/{pid}/cmdline")
    real_read_bytes = Path.read_bytes

    def racing_read_bytes(self: Path) -> bytes:
        if self == cmdline_path:
            raise FileNotFoundError(str(self))
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", racing_read_bytes)
    (build / "bitbake.lock").write_text(f"{pid}\n")

    assert kas_lock._lock_pid_is_live_bitbake(pid) is True
    # Unverifiable as this build's idle cooker, so the probe fails closed.
    assert kas_lock.local_build_holder(cfg) == pid


def test_marker_names_host_non_utf8_marker_is_not_a_match(tmp_path: Path) -> None:
    from bakar.steps.kas_lock import _marker_names_host

    marker = tmp_path / ".bakar-lock-host"
    marker.write_bytes(b"\xff\xfehost")
    assert _marker_names_host(marker, "host") is False


def _tmpfiles(marker: Path) -> list[str]:
    return [p.name for p in marker.parent.iterdir() if p.name.startswith(f".{marker.name}.")]


def _raise_oserror(code: int) -> Callable[..., None]:
    def fail(*_a: object, **_k: object) -> None:
        raise OSError(code, os.strerror(code))

    return fail


@pytest.mark.parametrize(
    ("target", "attr", "code"),
    [
        ("tempfile", "mkstemp", errno.EDQUOT),
        ("os", "fdopen", errno.ENOSPC),
        ("pathlib", "chmod", errno.EDQUOT),
        ("pathlib", "mkdir", errno.EIO),
    ],
)
def test_marker_file_operation_failure_is_a_refusal_naming_marker_and_errno(
    env: tuple[BuildConfig, Path, RunLogger],
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    attr: str,
    code: int,
) -> None:
    import pathlib
    import tempfile

    cfg, _build, log = env
    marker = build_stop.lock_marker_path(cfg)
    owner = {"tempfile": tempfile, "os": os, "pathlib": pathlib.Path}[target]
    monkeypatch.setattr(owner, attr, _raise_oserror(code))
    with pytest.raises(LockUnattributableError) as ei, lock_owner_marker(cfg, log):
        pass
    msg = str(ei.value)
    assert str(marker) in msg
    assert os.strerror(code) in msg
    assert "nothing was published" in msg
    assert "delete the marker" in msg
    monkeypatch.undo()
    assert not marker.exists()
    assert _tmpfiles(marker) == []


def test_own_marker_is_not_replaced_so_a_replace_failure_cannot_touch_it(
    env: tuple[BuildConfig, Path, RunLogger], monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, _build, log = env
    marker = build_stop.lock_marker_path(cfg)
    marker.write_text("this-host")
    monkeypatch.setattr(os, "replace", _raise_oserror(errno.EIO))
    with lock_owner_marker(cfg, log):
        assert marker.read_text() == "this-host"


def test_garbled_marker_replace_lost_reply_reads_back_and_proceeds(
    env: tuple[BuildConfig, Path, RunLogger], monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, _build, log = env
    marker = build_stop.lock_marker_path(cfg)
    marker.write_text("bad host!\n\x01")
    real_replace = os.replace

    def lost(src: object, dst: object) -> None:
        real_replace(src, dst)
        raise OSError(errno.EIO, "reply lost")

    monkeypatch.setattr(os, "replace", lost)
    with lock_owner_marker(cfg, log):
        assert marker.read_text() == "this-host"


def test_unknown_outcome_message_carries_the_errno_text(
    env: tuple[BuildConfig, Path, RunLogger],
) -> None:
    cfg, _build, _log = env
    marker = build_stop.lock_marker_path(cfg)
    exc = kas_lock.LockMarkerOutcomeUnknownError(marker, OSError(errno.EIO, "x"))
    assert os.strerror(errno.EIO) in str(exc)
    assert str(marker) in str(exc)


def test_own_marker_removal_failure_is_a_refusal_not_a_bare_oserror(
    env: tuple[BuildConfig, Path, RunLogger], monkeypatch: pytest.MonkeyPatch
) -> None:
    import pathlib

    cfg, _build, _log = env
    marker = build_stop.lock_marker_path(cfg)
    marker.write_text("this-host")
    monkeypatch.setattr(pathlib.Path, "unlink", _raise_oserror(errno.EIO))
    with pytest.raises(LockUnattributableError) as ei:
        kas_lock.clear_stale_bitbake_locks(cfg)
    assert str(marker) in str(ei.value)
    assert os.strerror(errno.EIO) in str(ei.value)


# ---------------------------------------------------------------------------
# clear_stale_bitbake_locks: a live launch record blocks rows 2 and 3
# ---------------------------------------------------------------------------


def _container_build(
    cfg: BuildConfig,
    build: Path,
    tmp_path: Path,
    live_child: Callable[..., AbstractContextManager[subprocess.Popen[bytes]]],
    *,
    container_alive: bool,
) -> tuple[Path, int]:
    """A container build's on-disk state: container-ns pid in the lock, sockets, own marker, record."""
    wrapper_pid = _dead_pid(live_child)
    (build / "bitbake.lock").write_text("4194000\n")
    (build / "bitbake.sock").write_text("")
    (build / "hashserve.sock").write_text("")
    marker = build_stop.lock_marker_path(cfg)
    marker.write_text("this-host\n")
    build_stop.write_launch_record(
        _run_dir(cfg),
        pgid=wrapper_pid,
        mode="container",
        runtime=str(_fake_runtime(tmp_path, container_alive=container_alive)),
        container_label="bakar.run_id=x",
    )
    return marker, wrapper_pid


def test_clear_keeps_the_lock_sockets_and_marker_of_a_live_container_build(
    env: tuple[BuildConfig, Path, RunLogger],
    tmp_path: Path,
    live_child: Callable[..., AbstractContextManager[subprocess.Popen[bytes]]],
) -> None:
    cfg, build, _log = env
    marker, wrapper_pid = _container_build(cfg, build, tmp_path, live_child, container_alive=True)

    outcome = kas_lock.clear_stale_bitbake_locks(cfg)

    assert outcome.removed == []
    assert outcome.refusal is not None
    assert outcome.refusal.reason == "held-locally"
    assert outcome.refusal.pid == wrapper_pid
    assert "bakar.run_id=x" in outcome.refusal.detail
    assert "bakar.run_id=x" in kas_lock._lock_refusal_message(outcome.refusal)
    assert marker.exists()
    assert {p.name for p in build.iterdir()} >= {"bitbake.lock", "bitbake.sock", "hashserve.sock"}


def test_clear_recovers_a_crashed_container_build_whose_record_is_not_live(
    env: tuple[BuildConfig, Path, RunLogger],
    tmp_path: Path,
    live_child: Callable[..., AbstractContextManager[subprocess.Popen[bytes]]],
) -> None:
    cfg, build, _log = env
    marker, _pid = _container_build(cfg, build, tmp_path, live_child, container_alive=False)

    outcome = kas_lock.clear_stale_bitbake_locks(cfg)

    assert outcome.refusal is None
    assert not marker.exists()
    assert not (build / "bitbake.lock").exists()
    assert not (build / "bitbake.sock").exists()


def test_clear_keeps_the_marker_of_a_build_in_its_checkout_phase(
    env: tuple[BuildConfig, Path, RunLogger],
    live_child: Callable[..., AbstractContextManager[subprocess.Popen[bytes]]],
) -> None:
    """Own marker, no lock yet, live host record: row 2 must not delete the marker."""
    cfg, _build, _log = env
    marker = build_stop.lock_marker_path(cfg)
    marker.write_text("this-host\n")
    with live_child("kas-container") as wrapper:
        build_stop.write_launch_record(_run_dir(cfg), pgid=wrapper.pid, mode="host")

        outcome = kas_lock.clear_stale_bitbake_locks(cfg)

        assert outcome.removed == []
        assert outcome.refusal is not None
        assert outcome.refusal.reason == "held-locally"
        assert outcome.refusal.pid == wrapper.pid
        assert marker.exists()


def test_clear_removes_the_marker_when_the_checkout_phase_build_crashed(
    env: tuple[BuildConfig, Path, RunLogger],
    live_child: Callable[..., AbstractContextManager[subprocess.Popen[bytes]]],
) -> None:
    cfg, _build, _log = env
    marker = build_stop.lock_marker_path(cfg)
    marker.write_text("this-host\n")
    build_stop.write_launch_record(_run_dir(cfg), pgid=_dead_pid(live_child), mode="host")

    outcome = kas_lock.clear_stale_bitbake_locks(cfg)

    assert outcome.refusal is None
    assert not marker.exists()


def test_clear_does_not_refuse_a_fresh_build_whose_own_run_has_no_record_yet(
    env: tuple[BuildConfig, Path, RunLogger],
) -> None:
    """The gate runs before the build writes its launch record: the caller's own run dir holds none."""
    cfg, build, _log = env
    build_stop.lock_marker_path(cfg).write_text("this-host\n")
    (build / "bitbake.sock").write_text("")

    outcome = kas_lock.clear_stale_bitbake_locks(cfg)

    assert outcome.refusal is None
    assert not (build / "bitbake.sock").exists()


def _older_container_record(
    cfg: BuildConfig,
    tmp_path: Path,
    wrapper_pid: int,
    *,
    alive: bool,
    age_minutes: int,
) -> None:
    started = datetime.now(UTC) - timedelta(minutes=age_minutes)
    build_stop.write_launch_record(
        _run_dir(cfg, started=started, pid=wrapper_pid),
        pgid=wrapper_pid,
        mode="container",
        runtime=str(_fake_runtime(tmp_path, container_alive=alive)),
        container_label="bakar.run_id=x",
    )


def test_local_build_holder_finds_a_live_record_behind_the_callers_own_empty_run_dir(
    env: tuple[BuildConfig, Path, RunLogger],
    tmp_path: Path,
    bitbake_server: Callable[..., AbstractContextManager[subprocess.Popen[bytes]]],
    live_child: Callable[..., AbstractContextManager[subprocess.Popen[bytes]]],
) -> None:
    """``clean --build-dir`` opens its own RunLogger first: the newest dir holds no record."""
    cfg, build, _log = env
    container_pid = _dead_pid(live_child)
    wrapper_pid = _dead_pid(live_child)
    _older_container_record(cfg, tmp_path, wrapper_pid, alive=True, age_minutes=5)
    _run_dir(cfg, pid=4242)  # the caller's own, empty
    with bitbake_server("/work/build", children="worker"):
        (build / "bitbake.lock").write_text(f"{container_pid}\n")
        assert kas_lock.local_build_holder(cfg) == wrapper_pid


def test_local_build_holder_keeps_a_live_record_behind_eight_newer_run_dirs(
    env: tuple[BuildConfig, Path, RunLogger],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bitbake_server: Callable[..., AbstractContextManager[subprocess.Popen[bytes]]],
    live_child: Callable[..., AbstractContextManager[subprocess.Popen[bytes]]],
) -> None:
    """Eight newer runs used to push a live container build out of an 8-directory window.

    This inverts the earlier window-drop assertion: the requirement is now that
    no count of newer runs hides a live build, so the lock is kept.
    """
    cfg, build, _log = env
    _on_nfs(monkeypatch, False)
    container_pid = _dead_pid(live_child)
    wrapper_pid = _dead_pid(live_child)
    _older_container_record(cfg, tmp_path, wrapper_pid, alive=True, age_minutes=60)
    for i in range(8):
        _run_dir(cfg, started=datetime.now(UTC) - timedelta(minutes=i), pid=5000 + i)
    with bitbake_server("/work/build", children="worker"):
        (build / "bitbake.lock").write_text(f"{container_pid}\n")
        assert kas_lock.local_build_holder(cfg) == wrapper_pid

        outcome = kas_lock.clear_stale_bitbake_locks(cfg)
        assert outcome.refusal is not None
        assert outcome.refusal.reason == "held-locally"
        assert outcome.removed == []
        assert (build / "bitbake.lock").exists()


@pytest.mark.parametrize("depth", [9, 40])
def test_holder_scans_every_run_dir(
    env: tuple[BuildConfig, Path, RunLogger],
    live_child: Callable[..., AbstractContextManager[subprocess.Popen[bytes]]],
    depth: int,
) -> None:
    """A live record under the ``depth``-th newest run directory is still a holder."""
    cfg, build, _log = env
    now = datetime.now(UTC)
    (build / "bitbake.lock").write_text(f"{_dead_pid(live_child)}\n")
    with live_child("kas-container") as wrapper:
        live_dir = _run_dir(cfg, started=now - timedelta(minutes=depth), pid=6000)
        build_stop.write_launch_record(live_dir, pgid=wrapper.pid, mode="host")
        for i in range(depth - 1):
            newer = _run_dir(cfg, started=now - timedelta(minutes=i), pid=5000 + i)
            if i % 2:  # finished runs leave a dead record behind; others never wrote one
                build_stop.write_launch_record(newer, pgid=_dead_pid(live_child), mode="host")
        names = sorted((d.name for d in cfg.runs_dir.iterdir()), reverse=True)
        assert names.index(live_dir.name) == depth - 1
        assert kas_lock.local_build_holder(cfg) == wrapper.pid


def test_holder_lock_absent_live_record(
    env: tuple[BuildConfig, Path, RunLogger],
    tmp_path: Path,
    live_child: Callable[..., AbstractContextManager[subprocess.Popen[bytes]]],
) -> None:
    """kas writes the record before bitbake creates the lock: no lock yet is still a build."""
    cfg, build, _log = env
    assert not (build / "bitbake.lock").exists()
    with live_child("kas-container") as wrapper:
        build_stop.write_launch_record(_run_dir(cfg), pgid=wrapper.pid, mode="host")
        assert kas_lock.local_build_holder(cfg) == wrapper.pid

    container_dir = _run_dir(cfg, started=datetime.now(UTC) + timedelta(minutes=1), pid=4343)
    wrapper_pid = _dead_pid(live_child)
    build_stop.write_launch_record(
        container_dir,
        pgid=wrapper_pid,
        mode="container",
        runtime=str(_fake_runtime(tmp_path, container_alive=True)),
        container_label="bakar.run_id=x",
    )
    assert kas_lock.local_build_holder(cfg) == wrapper_pid

    _fake_runtime(tmp_path, container_alive=False)
    assert kas_lock.local_build_holder(cfg) is None  # every record dead and no lock: no holder


def test_holder_idle_cooker_without_record_is_not_a_holder_behind_dead_records(
    env: tuple[BuildConfig, Path, RunLogger],
    bitbake_server: Callable[..., AbstractContextManager[subprocess.Popen[bytes]]],
    live_child: Callable[..., AbstractContextManager[subprocess.Popen[bytes]]],
) -> None:
    """Scanning every run must not turn a warm idle cooker into a holder."""
    cfg, build, _log = env
    for i in range(12):
        run_dir = _run_dir(cfg, started=datetime.now(UTC) - timedelta(minutes=i), pid=5000 + i)
        build_stop.write_launch_record(run_dir, pgid=_dead_pid(live_child), mode="host")
    with bitbake_server(build) as server:
        (build / "bitbake.lock").write_text(f"{server.pid}\n")
        assert kas_lock.local_build_holder(cfg) is None


def test_holder_container_listing_queried_once(
    env: tuple[BuildConfig, Path, RunLogger],
    tmp_path: Path,
    live_child: Callable[..., AbstractContextManager[subprocess.Popen[bytes]]],
) -> None:
    """Two stale container records under one runtime cost one runtime call, not one each."""
    cfg, build, _log = env
    runtime = str(_fake_runtime(tmp_path, container_alive=False))
    for i, label in enumerate(("bakar.run_id=a", "bakar.run_id=b")):
        build_stop.write_launch_record(
            _run_dir(cfg, started=datetime.now(UTC) - timedelta(minutes=i), pid=5000 + i),
            pgid=_dead_pid(live_child),
            mode="container",
            runtime=runtime,
            container_label=label,
        )
    (build / "bitbake.lock").write_text(f"{_dead_pid(live_child)}\n")

    assert kas_lock.local_build_holder(cfg) is None
    calls = (tmp_path / "calls.log").read_text().splitlines()
    assert len(calls) == 1
    assert "label=bakar.run_id" in calls[0]


def test_live_refusal_names_an_unanswering_runtime(
    env: tuple[BuildConfig, Path, RunLogger],
    tmp_path: Path,
    live_child: Callable[..., AbstractContextManager[subprocess.Popen[bytes]]],
) -> None:
    """A runtime that cannot answer keeps the record live, and the refusal says what to do."""
    cfg, build, _log = env
    runtime = str(_failing_runtime(tmp_path))
    wrapper_pid = _dead_pid(live_child)
    build_stop.write_launch_record(
        _run_dir(cfg), pgid=wrapper_pid, mode="container", runtime=runtime, container_label="bakar.run_id=x"
    )
    build_stop.lock_marker_path(cfg).write_text("this-host\n")
    (build / "bitbake.lock").write_text("4194000\n")

    assert kas_lock.local_build_holder(cfg) == wrapper_pid
    outcome = kas_lock.clear_stale_bitbake_locks(cfg)
    assert outcome.refusal is not None
    assert outcome.refusal.pid == wrapper_pid
    message = kas_lock._lock_refusal_message(outcome.refusal)
    assert runtime in message
    assert f"start {runtime}" in message
    assert "bakar stop" in message
    assert (build / "bitbake.lock").exists()


def test_clear_stale_row5_keeps_lock_with_live_record(
    env: tuple[BuildConfig, Path, RunLogger],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    live_child: Callable[..., AbstractContextManager[subprocess.Popen[bytes]]],
) -> None:
    """No marker, local filesystem, container-ns pid in the lock: the live record keeps the lock."""
    cfg, build, _log = env
    _on_nfs(monkeypatch, False)
    marker, wrapper_pid = _container_build(cfg, build, tmp_path, live_child, container_alive=True)
    marker.unlink()

    outcome = kas_lock.clear_stale_bitbake_locks(cfg)

    assert outcome.removed == []
    assert outcome.refusal is not None
    assert outcome.refusal.reason == "held-locally"
    assert outcome.refusal.pid == wrapper_pid
    assert {p.name for p in build.iterdir()} >= {"bitbake.lock", "bitbake.sock", "hashserve.sock"}

    _fake_runtime(tmp_path, container_alive=False)  # the container exited: row 5 recovers as before
    outcome = kas_lock.clear_stale_bitbake_locks(cfg)
    assert outcome.refusal is None
    assert not (build / "bitbake.lock").exists()


def test_local_build_holder_ignores_a_finished_record_in_an_older_run_dir(
    env: tuple[BuildConfig, Path, RunLogger],
    tmp_path: Path,
    bitbake_server: Callable[..., AbstractContextManager[subprocess.Popen[bytes]]],
    live_child: Callable[..., AbstractContextManager[subprocess.Popen[bytes]]],
) -> None:
    cfg, build, _log = env
    container_pid = _dead_pid(live_child)
    wrapper_pid = _dead_pid(live_child)
    _older_container_record(cfg, tmp_path, wrapper_pid, alive=False, age_minutes=5)
    _run_dir(cfg, pid=4242)
    with bitbake_server("/work/build", children="worker"):
        (build / "bitbake.lock").write_text(f"{container_pid}\n")
        assert kas_lock.local_build_holder(cfg) is None


@pytest.mark.parametrize(
    ("filename", "content"),
    [
        ("build.pid", b"99999999999\n"),
        ("build.pid", b"\xff\xfe\n"),
        ("build.meta.json", b'\xff{"pgid": 1, "mode": "container"}'),
    ],
)
def test_local_build_holder_warns_and_proceeds_on_an_unreadable_launch_record(
    env: tuple[BuildConfig, Path, RunLogger],
    capsys: pytest.CaptureFixture[str],
    filename: str,
    content: bytes,
) -> None:
    cfg, _build, _log = env
    run_dir = _run_dir(cfg, pid=4242)
    (run_dir / filename).write_bytes(content)

    assert kas_lock.local_build_holder(cfg) is None

    err = capsys.readouterr().err
    assert str(run_dir / filename) in err


def test_proc_comm_of_invalid_utf8_does_not_raise(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    comm = tmp_path / "comm"
    comm.write_bytes(b"Pars\xffer-1\n")
    real_path = kas_lock.Path
    monkeypatch.setattr(kas_lock, "Path", lambda _p: real_path(comm))

    assert kas_lock._proc_comm(1).startswith("Pars")


def test_a_recorded_runtime_that_is_not_docker_or_podman_is_never_run(
    env: tuple[BuildConfig, Path, RunLogger],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg, _build, _log = env
    run_dir = _run_dir(cfg, pid=4242)
    meta = run_dir / "build.meta.json"
    meta.write_text('{"pgid": 1, "mode": "container", "runtime": "/tmp/x", "container_label": "bakar.run_id=x"}')
    argvs: list[object] = []

    def spy(args: object, *_a: object, **_k: object) -> None:
        argvs.append(args)
        raise OSError("not run")

    monkeypatch.setattr("bakar.stop_container.subprocess.run", spy)
    monkeypatch.setattr("bakar.stop_container.detect_runtime", lambda: "docker")

    assert build_stop.read_launch_record(run_dir).runtime is None
    assert build_stop.unreadable_launch_files(run_dir) == [meta]
    kas_lock.local_build_holder(cfg)

    assert not any("/tmp/x" in str(a) for a in argvs)
    assert str(meta) in capsys.readouterr().err


@pytest.mark.parametrize("victim", ["bitbake.lock", "bitbake.sock", "hashserve.sock"])
def test_undeletable_lock_or_socket_is_a_refusal_naming_path_and_errno(
    env: tuple[BuildConfig, Path, RunLogger], monkeypatch: pytest.MonkeyPatch, victim: str
) -> None:
    cfg, build, _log = env
    _on_nfs(monkeypatch, False)
    # A reaped child's pid is dead, so the stale branch reaches the removal.
    child = subprocess.Popen(["true"])
    child.wait()
    (build / "bitbake.lock").write_text(f"{child.pid}\n")
    (build / "bitbake.sock").write_text("")
    (build / "hashserve.sock").write_text("")
    real_unlink = Path.unlink

    def deny(self: Path, missing_ok: bool = False) -> None:
        if self.name == victim:
            raise PermissionError(errno.EACCES, "denied")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", deny)
    with pytest.raises(LockMarkerFileError) as excinfo:
        kas_lock.clear_stale_bitbake_locks(cfg)
    text = peer_lock_reason(excinfo.value)
    assert str(build / victim) in text
    assert os.strerror(errno.EACCES) in text


def test_exit_unlink_failure_text_is_escaped_once(
    env: tuple[BuildConfig, Path, RunLogger], monkeypatch: pytest.MonkeyPatch
) -> None:
    from rich.text import Text

    cfg, _build, log = env
    marker = build_stop.lock_marker_path(cfg)
    warnings: list[str] = []
    monkeypatch.setattr(log, "warn", lambda msg, **_f: warnings.append(msg))
    real = Path.unlink

    def fake(self: Path, *a: object, **k: object) -> None:
        if self.name == marker.name:
            raise OSError("bad [/x]")
        real(self, *a, **k)  # type: ignore[arg-type]

    with lock_owner_marker(cfg, log):
        monkeypatch.setattr(Path, "unlink", fake)
    assert len(warnings) == 1
    plain = Text.from_markup(warnings[0]).plain
    assert plain.endswith(": bad [/x]")
    assert "\\" not in plain
