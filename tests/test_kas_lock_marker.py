"""Tests for the one-step ownership marker and the unreadable-marker refusal."""

from __future__ import annotations

import errno
import os
import socket
from pathlib import Path

import pytest

from bakar import build_stop
from bakar.config import BuildConfig
from bakar.observability import RunLogger
from bakar.steps import kas_lock
from bakar.steps.kas_lock import (
    LockHeldByPeerError,
    LockMarkerUnsupportedError,
    LockUnattributableError,
    lock_owner_marker,
)


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
    monkeypatch.setattr("bakar.mounts.is_path_on_nfs", lambda _p: value)


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
    from bakar.steps.kas_build import _peer_lock_reason

    marker = Path("/b/\x1b]0;x\x07.bakar-lock-host")
    reason = _peer_lock_reason(LockMarkerUnsupportedError(marker, OSError(errno.EPERM, "x")))
    assert "\x1b" not in reason
    assert "hard links" in reason


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
    marker.write_text("this-host")

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
