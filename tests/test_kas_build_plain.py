"""Tests for the plain-mode frame controller in kas_build, plus unit tests
locking in the terminated-ordering fix (task 2.1) and the run_shell /
run_shell_capture step_fail-on-nonzero-rc fix (tasks 2.3/2.4).
"""

from __future__ import annotations

import json
import socket
import threading
from io import StringIO
from typing import TYPE_CHECKING

import pytest
from rich.console import Console
from rich.table import Table

from bakar import build_stop
from bakar.config import BuildConfig
from bakar.observability import RunLogger
from bakar.steps import kas_build
from bakar.steps.kas_build import BuildUIState, KasBuildContext, _PlainFrameController
from bakar.steps.kas_lock import LockHeldByPeerError

if TYPE_CHECKING:
    from pathlib import Path

_ESC = "\x1b"


def _controller(buf: StringIO) -> _PlainFrameController:
    console = Console(no_color=True, force_terminal=False, file=buf)
    stop = threading.Event()
    stop.set()  # loop exits on the first wait, so the thread joins immediately
    return _PlainFrameController(BuildUIState(start_monotonic=1.0), console, stop)


def test_shim_start_refresh_and_transient_do_not_raise() -> None:
    # R1: the restart path calls live.start(refresh=True) and writes live.transient.
    with _controller(StringIO()) as live:
        live.stop()
        live.start(refresh=True)
        live.transient = True


def test_shim_renders_table_plainly() -> None:
    # The closures pass Rich Table renderables (layer_hash_table); a no-color
    # console must render them to plain text, not repr, and emit no ANSI.
    buf = StringIO()
    with _controller(buf) as live:
        table = Table()
        table.add_column("layer")
        table.add_row("layerhash123")
        live.console.print(table)
    out = buf.getvalue()
    assert _ESC not in out
    assert "layerhash123" in out
    assert "rich.table.Table" not in out


def test_shim_console_prints_plain_status_without_ansi() -> None:
    # The status line has literal brackets ("bakar[build]"); the loop prints with
    # markup=False so Rich does not eat them as a style tag. Mirror that here.
    buf = StringIO()
    with _controller(buf) as live:
        live.console.print("bakar[build] phase=tasks tasks=1/2 running=1 elapsed=0s", markup=False)
    out = buf.getvalue()
    assert _ESC not in out
    assert "bakar[build]" in out
    assert "phase=tasks" in out


# ---------------------------------------------------------------------------
# Shared helpers for the run_shell / run_shell_capture / run_build tests below
# ---------------------------------------------------------------------------


def _make_cfg(workspace: Path) -> BuildConfig:
    """Construct a minimal container-mode NXP BuildConfig rooted at ``workspace``.

    These tests assert step_ok/step_fail event bookkeeping around the shell and
    build wrappers, which is mode-independent. The container path is pinned so
    they do not have to provision the bitbake bin directory the host path
    requires.
    """
    return BuildConfig(
        workspace=workspace,
        host_mode=False,
        bsp_family="nxp",
        machine="imx95-var-dart",
        distro="fsl-imx-xwayland",
        image="core-image-minimal",
        manifest="imx-6.12.49-2.2.0.xml",
        repo_url="https://example.invalid/none.git",
        repo_branch="walnascar",
        kas_container_image="jetm/kas-build-env:latest",
    )


def _prepare_workspace(tmp_path: Path) -> tuple[BuildConfig, Path, Path]:
    """Build a cfg plus a kas YAML inside bsp_root and an overlay source."""
    cfg = _make_cfg(tmp_path)
    cfg.bsp_root.mkdir(parents=True, exist_ok=True)
    kas_yaml = cfg.bsp_root / "build.yml"
    kas_yaml.write_text("header: {}\n", encoding="utf-8")
    overlay = tmp_path / "bakar-tuning-nxp.yml"
    overlay.write_text("header: {}\n", encoding="utf-8")
    return cfg, kas_yaml, overlay


def _make_ctx(tmp_path: Path, log: RunLogger) -> KasBuildContext:
    cfg, kas_yaml, overlay = _prepare_workspace(tmp_path)
    return KasBuildContext(cfg=cfg, log=log, kas_yaml=kas_yaml, overlay_source=overlay)


class _FakeProc:
    """Stand-in for ``subprocess.Popen`` that returns a fixed exit code."""

    def __init__(self, rc: int) -> None:
        self._rc = rc

    def wait(self, timeout: float | None = None) -> int:
        # ``run_shell_capture`` always passes a timeout keyword, even when it is
        # None, so the real Popen signature has to be mirrored here.
        return self._rc


def _step_events(events_path: Path) -> list[dict]:
    """Parse events.jsonl, keeping only step_* records (matches test_observability.py)."""
    parsed = [json.loads(ln) for ln in events_path.read_text().splitlines() if ln]
    return [e for e in parsed if e.get("event") in {"step_start", "step_ok", "step_fail", "step_skip"}]


# ---------------------------------------------------------------------------
# task 2.3 / 2.4: run_shell and run_shell_capture emit step_fail on nonzero rc
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("rc", [0, 1, 137])
def test_run_shell_emits_step_ok_or_step_fail_matching_rc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rc: int
) -> None:
    monkeypatch.setattr(kas_build.subprocess, "Popen", lambda *args, **kwargs: _FakeProc(rc))

    with RunLogger(runs_dir=tmp_path / "runs") as log:
        ctx = _make_ctx(tmp_path, log)
        result = kas_build.run_shell(ctx, args=[])
        events_path = log.events_path

    assert result == rc
    events = _step_events(events_path)
    terminal = [e for e in events if e["step"] == "kas_shell" and e["event"] != "step_start"]
    assert len(terminal) == 1, f"expected exactly one terminal kas_shell event, got {terminal!r}"
    if rc == 0:
        assert terminal[0]["event"] == "step_ok"
    else:
        assert terminal[0]["event"] == "step_fail"
        assert terminal[0].get("reason") == f"exit_code={rc}"
        assert terminal[0].get("exit_code") == rc


@pytest.mark.unit
@pytest.mark.parametrize("rc", [0, 1, 137])
def test_run_shell_capture_emits_step_ok_or_step_fail_matching_rc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rc: int
) -> None:
    # Not exercising the cache-mount/daemon-state launch gate here - it probes
    # real subprocesses via bakar.mounts, which would otherwise pick up the
    # global subprocess.Popen patch below meant only for the kas launch itself.
    monkeypatch.setattr(kas_build, "cache_mount_refusal", lambda _cfg, **_kw: None)
    # clear_stale_bitbake_locks's is_path_on_nfs classification also spawns a
    # real ``realpath`` child now (bug 1's bounded, symlink-following
    # resolution) - stub it directly so the same global Popen patch below,
    # meant only for the kas launch itself, is not also asked to stand in for
    # that unrelated subprocess call.
    monkeypatch.setattr("bakar.mounts.is_path_on_nfs", lambda _p: False)
    monkeypatch.setattr(kas_build.subprocess, "Popen", lambda *args, **kwargs: _FakeProc(rc))

    with RunLogger(runs_dir=tmp_path / "runs") as log:
        ctx = _make_ctx(tmp_path, log)
        stdout_path = tmp_path / "capture.log"
        result = kas_build.run_shell_capture(
            ctx, command="bitbake -c listtasks foo", capture=kas_build.ShellCapture(stdout_path=stdout_path)
        )
        events_path = log.events_path

    assert result == rc
    events = _step_events(events_path)
    terminal = [e for e in events if e["step"] == "kas_shell_capture" and e["event"] != "step_start"]
    assert len(terminal) == 1, f"expected exactly one terminal kas_shell_capture event, got {terminal!r}"
    if rc == 0:
        assert terminal[0]["event"] == "step_ok"
    else:
        assert terminal[0]["event"] == "step_fail"
        assert terminal[0].get("reason") == f"exit_code={rc}"
        assert terminal[0].get("exit_code") == rc


# ---------------------------------------------------------------------------
# task 2.1: terminated=True set before the persistence tail - no duplicate
# terminal kas_build event when the persistence tail raises
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_run_build_persist_tail_failure_does_not_duplicate_terminal_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, kas_yaml, overlay = _prepare_workspace(tmp_path)

    def _fake_run_pty(*args: object, **kwargs: object) -> kas_build._PtyOutcome:
        return kas_build._PtyOutcome(rc=0)

    def _boom_persist(*_args: object, **_kwargs: object) -> object:
        raise OSError("disk full")

    monkeypatch.setattr(kas_build, "_run_pty_with_ui", _fake_run_pty)
    monkeypatch.setattr(kas_build, "copy_oe_eventlog_to_run_dir", _boom_persist)

    with RunLogger(runs_dir=cfg.runs_dir) as log:
        ctx = KasBuildContext(cfg=cfg, log=log, kas_yaml=kas_yaml, overlay_source=overlay)
        rc = kas_build.run_build(ctx)
        events_path = log.events_path

    assert rc == 0
    events = _step_events(events_path)
    terminal = [e for e in events if e["step"] == "kas_build" and e["event"] != "step_start"]
    assert len(terminal) == 1, f"expected exactly one terminal kas_build event, got {terminal!r}"
    assert terminal[0]["event"] == "step_ok"


# ---------------------------------------------------------------------------
# The post-build graph capture re-dumps the kas config; it must flatten the
# overlays the build ran with, not just the base tuning overlay.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_run_build_hands_the_builds_overlays_to_graph_capture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg, kas_yaml, overlay = _prepare_workspace(tmp_path)
    tuning = [tmp_path / "bakar-tuning-mold.yml", tmp_path / "bakar-tuning-hashequiv.yml"]
    for path in tuning:
        path.write_text("header:\n  version: 16\n", encoding="utf-8")
    seen: list[list[Path]] = []

    monkeypatch.setattr(kas_build, "_run_pty_with_ui", lambda *a, **k: kas_build._PtyOutcome(rc=0))
    monkeypatch.setattr(kas_build, "_capture_dependency_graph", lambda c, _log: seen.append(list(c.extra_overlays)))

    with RunLogger(runs_dir=cfg.runs_dir) as log:
        ctx = KasBuildContext(cfg=cfg, log=log, kas_yaml=kas_yaml, overlay_source=overlay)
        rc = kas_build.run_build(ctx, extra_overlays=tuning)

    assert rc == 0
    assert seen == [tuning]
    # The caller's context is left as it was: only the capture sees the overlays.
    assert ctx.extra_overlays == []


# ---------------------------------------------------------------------------
# task 5.1: run_shell takes the lock gate and marker only on an explicit opt-in
# ---------------------------------------------------------------------------


def _shell_lock_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[BuildConfig, Path, Path, Path]:
    """Config with an existing build dir, a confirmed-local filesystem and host 'this-host'."""
    monkeypatch.setattr(socket, "gethostname", lambda: "this-host")
    monkeypatch.setattr("bakar.mounts.is_path_on_nfs", lambda _p: False)
    cfg, kas_yaml, overlay = _prepare_workspace(tmp_path)
    (cfg.bsp_root / cfg.build_dir_name).mkdir(parents=True, exist_ok=True)
    return cfg, kas_yaml, overlay, build_stop.lock_marker_path(cfg)


class _MarkerProbingPopen:
    """Popen stand-in recording the marker content (None if absent) at each launch."""

    def __init__(self, marker: Path, rc: int = 0) -> None:
        self._marker = marker
        self._rc = rc
        self.launches: list[str | None] = []

    def __call__(self, *_args: object, **_kwargs: object) -> _FakeProc:
        self.launches.append(self._marker.read_text() if self._marker.exists() else None)
        return _FakeProc(self._rc)


@pytest.mark.unit
def test_run_shell_without_the_opt_in_takes_no_gate_or_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg, kas_yaml, overlay, marker = _shell_lock_env(tmp_path, monkeypatch)
    probe = _MarkerProbingPopen(marker)
    monkeypatch.setattr(kas_build.subprocess, "Popen", probe)
    gate_calls: list[object] = []
    monkeypatch.setattr(kas_build, "clear_stale_bitbake_locks", gate_calls.append)

    with RunLogger(runs_dir=tmp_path / "runs") as log:
        ctx = KasBuildContext(cfg=cfg, log=log, kas_yaml=kas_yaml, overlay_source=overlay)
        assert kas_build.run_shell(ctx, args=[], command="bitbake -e") == 0

    assert gate_calls == []
    assert probe.launches == [None]
    assert not marker.exists()


@pytest.mark.unit
def test_run_shell_with_the_opt_in_writes_the_marker_around_the_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, kas_yaml, overlay, marker = _shell_lock_env(tmp_path, monkeypatch)
    probe = _MarkerProbingPopen(marker)
    monkeypatch.setattr(kas_build.subprocess, "Popen", probe)

    with RunLogger(runs_dir=tmp_path / "runs") as log:
        ctx = KasBuildContext(cfg=cfg, log=log, kas_yaml=kas_yaml, overlay_source=overlay)
        assert kas_build.run_shell(ctx, args=[], command="bitbake -e", take_lock_marker=True) == 0

    assert probe.launches == ["this-host"]


@pytest.mark.unit
def test_run_shell_with_the_opt_in_refuses_on_a_peer_held_lock_without_launching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, kas_yaml, overlay, marker = _shell_lock_env(tmp_path, monkeypatch)
    marker.write_text("peer-host")
    probe = _MarkerProbingPopen(marker)
    monkeypatch.setattr(kas_build.subprocess, "Popen", probe)

    with RunLogger(runs_dir=tmp_path / "runs") as log:
        ctx = KasBuildContext(cfg=cfg, log=log, kas_yaml=kas_yaml, overlay_source=overlay)
        rc = kas_build.run_shell(ctx, args=[], command="bitbake -e", take_lock_marker=True)
        events_path = log.events_path

    assert rc == 1
    assert probe.launches == []
    assert marker.read_text() == "peer-host"
    fails = [e for e in _step_events(events_path) if e["step"] == "kas_shell" and e["event"] == "step_fail"]
    assert len(fails) == 1
    assert "peer-host" in fails[0]["reason"]
    assert fails[0]["exit_code"] == 1


@pytest.mark.unit
def test_run_shell_lock_marker_race_refusal_records_exit_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg, kas_yaml, overlay, _marker = _shell_lock_env(tmp_path, monkeypatch)

    class _PeerWins:
        def __init__(self, *_a: object, **_k: object) -> None:
            pass

        def __enter__(self) -> None:
            raise LockHeldByPeerError("peer-host")

        def __exit__(self, *_exc: object) -> None:
            return None

    monkeypatch.setattr(kas_build, "lock_owner_marker", _PeerWins)

    with RunLogger(runs_dir=tmp_path / "runs") as log:
        ctx = KasBuildContext(cfg=cfg, log=log, kas_yaml=kas_yaml, overlay_source=overlay)
        rc = kas_build.run_shell(ctx, args=[], command="bitbake -e", take_lock_marker=True)
        events_path = log.events_path

    assert rc == 1
    fails = [e for e in _step_events(events_path) if e["step"] == "kas_shell" and e["event"] == "step_fail"]
    assert len(fails) == 1
    assert fails[0]["exit_code"] == 1


@pytest.mark.unit
def test_run_shell_with_the_opt_in_removes_the_marker_when_the_lock_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, kas_yaml, overlay, marker = _shell_lock_env(tmp_path, monkeypatch)
    probe = _MarkerProbingPopen(marker, rc=3)
    monkeypatch.setattr(kas_build.subprocess, "Popen", probe)

    with RunLogger(runs_dir=tmp_path / "runs") as log:
        ctx = KasBuildContext(cfg=cfg, log=log, kas_yaml=kas_yaml, overlay_source=overlay)
        rc = kas_build.run_shell(ctx, args=[], take_lock_marker=True)

    assert rc == 3
    assert probe.launches == ["this-host"]
    assert not marker.exists()


@pytest.mark.unit
def test_run_shell_unexpected_oserror_records_step_fail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg, kas_yaml, overlay, _marker = _shell_lock_env(tmp_path, monkeypatch)

    def boom(*_a: object, **_k: object) -> None:
        raise OSError(5, "EIO")

    monkeypatch.setattr(kas_build.subprocess, "Popen", boom)
    with RunLogger(runs_dir=tmp_path / "runs") as log:
        ctx = KasBuildContext(cfg=cfg, log=log, kas_yaml=kas_yaml, overlay_source=overlay)
        with pytest.raises(OSError, match="EIO"):
            kas_build.run_shell(ctx, args=[], command="bitbake -e", take_lock_marker=True)
        events_path = log.events_path
    fails = [e for e in _step_events(events_path) if e["step"] == "kas_shell" and e["event"] == "step_fail"]
    assert len(fails) == 1
    assert fails[0]["exit_code"] == 1


@pytest.mark.unit
def test_run_shell_capture_marker_refusal_from_the_stale_gate_records_step_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bakar.steps.kas_lock import LockMarkerFileError

    cfg, kas_yaml, overlay, marker = _shell_lock_env(tmp_path, monkeypatch)

    def gate(_cfg: object) -> None:
        raise LockMarkerFileError(marker, OSError(5, "EIO"), "remove the marker")

    monkeypatch.setattr(kas_build, "clear_stale_bitbake_locks", gate)
    with RunLogger(runs_dir=tmp_path / "runs") as log:
        ctx = KasBuildContext(cfg=cfg, log=log, kas_yaml=kas_yaml, overlay_source=overlay)
        cap = kas_build.ShellCapture(stdout_path=tmp_path / "out.log", step="getvar")
        rc = kas_build.run_shell_capture(ctx, "bitbake -e", cap)
        events_path = log.events_path
    assert rc == 1
    fails = [e for e in _step_events(events_path) if e["step"] == "getvar" and e["event"] == "step_fail"]
    assert len(fails) == 1
    assert fails[0]["exit_code"] == 1


@pytest.mark.unit
@pytest.mark.parametrize("entry", ["live", "build"])
def test_marker_refusal_from_the_stale_gate_closes_the_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    """run_shell_live and run_build turn a lock-removal refusal into rc 1 and a terminal step_fail."""
    from bakar.steps.kas_lock import LockMarkerFileError

    cfg, kas_yaml, overlay, _marker = _shell_lock_env(tmp_path, monkeypatch)
    sock = cfg.bsp_root / cfg.build_dir_name / "bitbake.sock"

    def gate(_cfg: object) -> None:
        raise LockMarkerFileError(
            sock, PermissionError(13, "denied"), "remove the stale bitbake socket", subject="socket"
        )

    monkeypatch.setattr(kas_build, "clear_stale_bitbake_locks", gate)
    step = "kas_shell_live" if entry == "live" else "kas_build"
    with RunLogger(runs_dir=tmp_path / "runs") as log:
        ctx = KasBuildContext(cfg=cfg, log=log, kas_yaml=kas_yaml, overlay_source=overlay)
        rc = kas_build.run_shell_live(ctx, "bitbake x") if entry == "live" else kas_build.run_build(ctx)
        events_path = log.events_path
    assert rc == 1
    events = _step_events(events_path)
    fails = [e for e in events if e["step"] == step and e["event"] == "step_fail"]
    assert len(fails) == 1
    assert fails[0]["exit_code"] == 1
    assert str(sock) in fails[0]["reason"]
    opened = {e["step"] for e in events if e["event"] == "step_start"}
    closed = {e["step"] for e in events if e["event"] in {"step_ok", "step_fail", "step_skip"}}
    assert opened <= closed


@pytest.mark.unit
def test_run_shell_unexpected_oserror_text_is_escaped_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from rich.text import Text

    cfg, kas_yaml, overlay, _marker = _shell_lock_env(tmp_path, monkeypatch)

    def gate(_cfg: object) -> None:
        raise OSError("bad [/x]")

    monkeypatch.setattr(kas_build, "clear_stale_bitbake_locks", gate)
    with RunLogger(runs_dir=tmp_path / "runs") as log:
        ctx = KasBuildContext(cfg=cfg, log=log, kas_yaml=kas_yaml, overlay_source=overlay)
        with pytest.raises(OSError, match="bad"):
            kas_build.run_shell(ctx, ["x"], "bitbake x", take_lock_marker=True)
        events_path = log.events_path
    fails = [e for e in _step_events(events_path) if e["step"] == "kas_shell" and e["event"] == "step_fail"]
    assert Text.from_markup(fails[0]["reason"]).plain == "unexpected OSError: bad [/x]"
