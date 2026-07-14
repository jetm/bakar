"""Unit tests for bakar.build_stop lifecycle helpers.

Covers the pidfile round-trip, the procfs-cmdline liveness probe in
``is_build_running``, the mode-aware ``stop_build`` dispatch (host PGID
SIGINT->grace->SIGTERM->SIGKILL escalation, container runtime stop, and the
legacy/untargetable run that signals nothing), and the unclean-stop scan in
``check_unclean_stop``. Every test is hermetic: no signals reach real
processes, ``time.sleep`` is monkeypatched to a no-op, and ``os.killpg``,
``_container_id``, and ``_stop_container`` are recorded rather than executed.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console

from bakar import build_stop

pytestmark = pytest.mark.unit


def _make_run_dir(bsp_root: Path, run_id: str = "20260618-120000") -> Path:
    """Create ``bsp_root/build/runs/<run_id>`` and return it."""
    run_dir = bsp_root / "build" / "runs" / run_id
    run_dir.mkdir(parents=True)
    return run_dir


# --- write_pid / remove_pid round-trip -------------------------------------


def test_write_pid_creates_decimal_pidfile(tmp_path: Path) -> None:
    """write_pid writes the decimal PGID followed by a newline."""
    build_stop.write_pid(tmp_path, 4242)

    pid_file = tmp_path / "build.pid"
    assert pid_file.exists()
    assert pid_file.read_text() == "4242\n"


def test_remove_pid_deletes_existing_file(tmp_path: Path) -> None:
    """remove_pid unlinks an existing build.pid."""
    build_stop.write_pid(tmp_path, 99)
    assert (tmp_path / "build.pid").exists()

    build_stop.remove_pid(tmp_path)

    assert not (tmp_path / "build.pid").exists()


def test_remove_pid_absent_is_noop(tmp_path: Path) -> None:
    """remove_pid on a missing build.pid does not raise."""
    build_stop.remove_pid(tmp_path)  # no file present
    assert not (tmp_path / "build.pid").exists()


# --- is_build_running -------------------------------------------------------


def test_is_build_running_live_pgid_wrong_cmdline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live PGID whose /proc cmdline lacks kas tokens -> cmdline_ok False.

    Mirrors ``test_is_build_running_cmdline_ok_true`` but with a non-kas cmdline:
    patch the ``/proc/<pgid>/cmdline`` read to a deterministic python/pytest value
    so the result never depends on whatever else shares this process group. Using
    the real leader cmdline is fragile - a concurrent ``bakar build`` in the same
    group makes the leader cmdline match ``kas`` and flips cmdline_ok to True.
    """
    pgid = os.getpgrp()
    build_stop.write_pid(tmp_path, pgid)
    proc_cmdline = Path(f"/proc/{pgid}/cmdline")
    real_read_bytes = Path.read_bytes

    def fake_read_bytes(self: Path) -> bytes:
        if self == proc_cmdline:
            return b"python3\x00-m\x00pytest\x00"
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", fake_read_bytes)

    live, pgid_out, cmdline_ok = build_stop.is_build_running(tmp_path)

    assert live is True
    assert pgid_out == pgid
    assert cmdline_ok is False


def test_is_build_running_dead_pid(tmp_path: Path) -> None:
    """A PID that does not exist reports live=False."""
    build_stop.write_pid(tmp_path, 9999999)

    live, pgid, cmdline_ok = build_stop.is_build_running(tmp_path)

    assert live is False
    assert pgid == 9999999
    assert cmdline_ok is False


def test_is_build_running_missing_pidfile(tmp_path: Path) -> None:
    """No build.pid -> not running, no pgid."""
    live, pgid, cmdline_ok = build_stop.is_build_running(tmp_path)

    assert live is False
    assert pgid is None
    assert cmdline_ok is False


def test_is_build_running_cmdline_ok_true(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live PGID whose /proc cmdline contains kas-container -> cmdline_ok True.

    The module reads the cmdline via ``Path.read_bytes()`` on
    ``/proc/<pgid>/cmdline``. Patch that exact seam: return a fake
    null-separated cmdline for the procfs path and defer to the real
    implementation for every other path so the pidfile read still works.
    """
    pgid = os.getpgrp()
    build_stop.write_pid(tmp_path, pgid)
    proc_cmdline = Path(f"/proc/{pgid}/cmdline")
    real_read_bytes = Path.read_bytes

    def fake_read_bytes(self: Path) -> bytes:
        if self == proc_cmdline:
            return b"kas-container\x00build\x00"
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", fake_read_bytes)

    live, pgid_out, cmdline_ok = build_stop.is_build_running(tmp_path)

    assert live is True
    assert pgid_out == pgid
    assert cmdline_ok is True


# --- stop_build -------------------------------------------------------------


def _record_killpg(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int]]:
    """Patch os.killpg to record (pgid, sig) calls instead of signalling."""
    calls: list[tuple[int, int]] = []

    def fake_killpg(pgid: int, sig: int) -> None:
        calls.append((pgid, sig))

    monkeypatch.setattr(build_stop.os, "killpg", fake_killpg)
    return calls


def test_stop_build_sigint_then_clean_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Host-mode force=False sends SIGINT; a quick PGID death stops escalation.

    The run is set up as a host launch record so stop_build takes the PGID path.
    is_build_running is stubbed to report a live, verified build. _pgid_alive
    returns False on the first grace poll so the loop exits before any
    escalation. remove_pid must run regardless.
    """
    run_dir = _make_run_dir(tmp_path)
    build_stop.write_launch_record(run_dir, pgid=4242, mode="host")

    monkeypatch.setattr(build_stop, "is_build_running", lambda _rd: (True, 4242, True))
    monkeypatch.setattr(build_stop, "_pgid_alive", lambda _pgid: False)
    monkeypatch.setattr(build_stop.time, "sleep", lambda _s: None)
    calls = _record_killpg(monkeypatch)

    assert build_stop.stop_build(tmp_path) is True

    assert calls == [(4242, signal.SIGINT)]
    assert not (run_dir / "build.pid").exists()
    assert not (run_dir / "build.meta.json").exists()


def test_stop_build_targets_older_live_run_when_newest_is_dead(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stop_build scans newest->oldest for the live build, not just runs[-1].

    A finished clean-recipe (or a second build) leaves a lexically-newer but
    dead run dir while an older build is still running. stop_build must target
    the older LIVE run rather than read the dead newest record and give up with
    'no running build found'.
    """
    older = _make_run_dir(tmp_path, "20260701-090000")
    newer = _make_run_dir(tmp_path, "20260701-100000")
    build_stop.write_launch_record(older, pgid=4242, mode="host")
    build_stop.write_launch_record(newer, pgid=5555, mode="host")

    def fake_running(run_dir: Path) -> tuple[bool, int | None, bool]:
        # Only the older run's build is alive; the newest is a finished run.
        if run_dir == older:
            return (True, 4242, True)
        return (False, 5555, False)

    monkeypatch.setattr(build_stop, "is_build_running", fake_running)
    monkeypatch.setattr(build_stop, "_pgid_alive", lambda _pgid: False)
    monkeypatch.setattr(build_stop.time, "sleep", lambda _s: None)
    calls = _record_killpg(monkeypatch)

    assert build_stop.stop_build(tmp_path) is True

    assert calls == [(4242, signal.SIGINT)]
    assert not (older / "build.pid").exists()


def test_escalate_host_sigterm_then_sigkill_when_lingering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_escalate_host sends SIGTERM then SIGKILL when the PGID survives the term wait."""
    monkeypatch.setattr(build_stop, "_pgid_alive", lambda _pgid: True)
    monkeypatch.setattr(build_stop.time, "sleep", lambda _s: None)
    calls = _record_killpg(monkeypatch)

    build_stop._escalate_host(4242)

    assert calls == [(4242, signal.SIGTERM), (4242, signal.SIGKILL)]


def test_escalate_host_no_sigkill_when_dead_after_sigterm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_escalate_host skips SIGKILL when the group is gone after SIGTERM."""
    monkeypatch.setattr(build_stop, "_pgid_alive", lambda _pgid: False)
    monkeypatch.setattr(build_stop.time, "sleep", lambda _s: None)
    calls = _record_killpg(monkeypatch)

    build_stop._escalate_host(4242)

    assert calls == [(4242, signal.SIGTERM)]


def test_stop_build_host_ctrl_c_runs_escalation_ladder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Ctrl-C during the host graceful wait escalates through SIGTERM -> SIGKILL.

    The unbounded wait no longer escalates on a timer; escalation is triggered
    only by the injected ``escalate`` callback firing. Stub ``_graceful_wait`` to
    invoke that callback (as a real Ctrl-C would) and assert the ladder runs.
    ``_pgid_alive`` is stateful: alive on the escalate SIGKILL-rung check (so the
    group is killed) then dead on the post-escalation verify (so `stop_build`
    confirms the tree is gone and returns True).
    """
    run_dir = _make_run_dir(tmp_path)
    build_stop.write_launch_record(run_dir, pgid=4242, mode="host")

    alive = {"n": 0}

    def _stateful_pgid_alive(_pgid: int) -> bool:
        alive["n"] += 1
        return alive["n"] == 1

    monkeypatch.setattr(build_stop, "is_build_running", lambda _rd: (True, 4242, True))
    monkeypatch.setattr(build_stop, "_pgid_alive", _stateful_pgid_alive)
    monkeypatch.setattr(build_stop.time, "sleep", lambda _s: None)

    def _fake_wait(*, escalate: object, **_kw: object) -> str:
        escalate()
        return "escalated"

    monkeypatch.setattr(build_stop, "_graceful_wait", _fake_wait)
    calls = _record_killpg(monkeypatch)

    assert build_stop.stop_build(tmp_path) is True

    sigs = [sig for _pgid, sig in calls]
    assert sigs == [signal.SIGINT, signal.SIGTERM, signal.SIGKILL]
    assert all(pgid == 4242 for pgid, _sig in calls)
    assert not (run_dir / "build.pid").exists()


def test_stop_build_grace_seconds_threaded_to_graceful_wait(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stop_build's grace_seconds reaches _graceful_wait unchanged."""
    run_dir = _make_run_dir(tmp_path)
    build_stop.write_launch_record(run_dir, pgid=4242, mode="host")

    monkeypatch.setattr(build_stop, "is_build_running", lambda _rd: (True, 4242, True))
    monkeypatch.setattr(build_stop, "_pgid_alive", lambda _pgid: False)
    monkeypatch.setattr(build_stop.time, "sleep", lambda _s: None)
    _record_killpg(monkeypatch)

    captured: dict[str, object] = {}

    def _fake_wait(*, grace_seconds: float = 0, **_kw: object) -> str:
        captured["grace_seconds"] = grace_seconds
        return "drained"

    monkeypatch.setattr(build_stop, "_graceful_wait", _fake_wait)

    assert build_stop.stop_build(tmp_path, grace_seconds=45) is True
    assert captured["grace_seconds"] == 45


def test_stop_build_force_skips_sigint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Host-mode force=True sends SIGTERM first and never sends SIGINT."""
    run_dir = _make_run_dir(tmp_path)
    build_stop.write_launch_record(run_dir, pgid=4242, mode="host")

    monkeypatch.setattr(build_stop, "is_build_running", lambda _rd: (True, 4242, True))
    monkeypatch.setattr(build_stop, "_pgid_alive", lambda _pgid: False)
    monkeypatch.setattr(build_stop.time, "sleep", lambda _s: None)
    calls = _record_killpg(monkeypatch)

    assert build_stop.stop_build(tmp_path, force=True) is True

    sigs = [sig for _pgid, sig in calls]
    assert signal.SIGINT not in sigs
    assert sigs[0] == signal.SIGTERM
    assert not (run_dir / "build.pid").exists()
    assert not (run_dir / "build.meta.json").exists()


def test_stop_build_no_running_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dead wrapper with no detached cooker is an idempotent clean-tree success.

    No wrapper, no argv-scoped cooker, and no live bitbake-server means nothing
    is signalled; ``stop_build`` clears any stale lock/sock and returns True
    (exit 0) rather than erroring, so a second ``bakar stop`` is a safe no-op.
    """
    run_dir = _make_run_dir(tmp_path)
    build_stop.write_launch_record(run_dir, pgid=4242, mode="host")

    monkeypatch.setattr(build_stop, "is_build_running", lambda _rd: (False, None, False))
    monkeypatch.setattr(build_stop.time, "sleep", lambda _s: None)
    calls = _record_killpg(monkeypatch)

    assert build_stop.stop_build(tmp_path) is True

    assert calls == []
    assert not (run_dir / "build.pid").exists()
    assert not (run_dir / "build.meta.json").exists()


# --- bitbake-server detached-PID liveness -----------------------------------
#
# bb.daemonize.createDaemon double-forks and calls os.setsid() to start
# bitbake-server, so it is never a member of the kas-container/kas process
# group build.pid records - killpg(pgid, ...) structurally cannot reach it.
# It writes its own os.getpid() as bitbake.lock's first line/token (see
# bitbake/lib/bb/server/process.py), which is the only reliable liveness
# signal for it. These tests reproduce the incident: something kills the
# wrapper (a stray SIGQUIT keypress, here) without ever signaling the server,
# which keeps dispatching already-queued tasks. Before this fix, stop_build's
# liveness gate only checked _pgid_alive(pgid), so it declared "stopped" and
# deleted bitbake.lock/bitbake.sock while the real cooker was still running.


def test_read_bitbake_server_pid_from_lock_file(tmp_path: Path) -> None:
    """The PID is read from bitbake.lock's first line, topdir = run_dir/../..."""
    run_dir = _make_run_dir(tmp_path)
    (run_dir.parent.parent / "bitbake.lock").write_text("424242\n")

    assert build_stop._read_bitbake_server_pid(run_dir) == 424242


def test_read_bitbake_server_pid_missing_lock_returns_none(tmp_path: Path) -> None:
    run_dir = _make_run_dir(tmp_path)

    assert build_stop._read_bitbake_server_pid(run_dir) is None


def test_read_bitbake_server_pid_unparseable_lock_returns_none(tmp_path: Path) -> None:
    """A corrupt or truncated lock file must not raise - just report unknown."""
    run_dir = _make_run_dir(tmp_path)
    (run_dir.parent.parent / "bitbake.lock").write_text("not-a-pid\n")

    assert build_stop._read_bitbake_server_pid(run_dir) is None


def test_pid_alive_true_for_current_process() -> None:
    assert build_stop._pid_alive(os.getpid()) is True


def test_pid_alive_false_for_reaped_process() -> None:
    """A PID that has already exited and been reaped reports not-alive."""
    proc = subprocess.Popen(["true"])
    proc.wait()

    assert build_stop._pid_alive(proc.pid) is False


def test_bitbake_server_alive_true_when_lock_pid_is_alive(tmp_path: Path) -> None:
    run_dir = _make_run_dir(tmp_path)
    (run_dir.parent.parent / "bitbake.lock").write_text(f"{os.getpid()}\n")

    assert build_stop._bitbake_server_alive(run_dir) is True


def test_bitbake_server_alive_false_when_lock_missing(tmp_path: Path) -> None:
    run_dir = _make_run_dir(tmp_path)

    assert build_stop._bitbake_server_alive(run_dir) is False


def test_stop_build_liveness_stays_alive_while_bitbake_server_pid_lives(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The liveness gate reports _ALIVE from a live bitbake.lock PID alone,
    even once the recorded PGID has already died - the exact incident this
    fixes: the wrapper is gone, but the detached cooker is not.
    """
    run_dir = _make_run_dir(tmp_path)
    build_stop.write_launch_record(run_dir, pgid=4242, mode="host")
    (run_dir.parent.parent / "bitbake.lock").write_text(f"{os.getpid()}\n")

    monkeypatch.setattr(build_stop, "is_build_running", lambda _rd: (True, 4242, True))
    monkeypatch.setattr(build_stop, "_pgid_alive", lambda _pgid: False)
    monkeypatch.setattr(build_stop.time, "sleep", lambda _s: None)
    _record_killpg(monkeypatch)
    # stop_build's initial SIGINT to bb_pid uses the real os.kill; bb_pid here
    # is this test process's own PID (to make _bitbake_server_alive's real
    # os.kill(pid, 0) probe see a genuinely live process), so a real SIGINT
    # would self-interrupt the test run. Stub it - the liveness lambda itself
    # is what this test is verifying, not the initial signal delivery.
    monkeypatch.setattr(build_stop.os, "kill", lambda _pid, _sig: None)

    # Call liveness() from inside the fake wait, mirroring how the real
    # unbounded loop uses it (repeatedly, before ever declaring drained) -
    # calling it after stop_build returns would see bitbake.lock already
    # deleted by the cleanup step that only runs once liveness reports dead.
    observed: list[str] = []

    def _fake_wait(*, liveness: object, **_kw: object) -> str:
        observed.append(liveness())  # type: ignore[misc]
        return "drained"

    monkeypatch.setattr(build_stop, "_graceful_wait", _fake_wait)

    assert build_stop.stop_build(tmp_path) is True
    assert observed == [build_stop._ALIVE]


def test_escalate_host_signals_bitbake_server_pid_too(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """force=True must reach bitbake-server's real PID, not just the PGID.

    Before this fix, --force only ever signalled the wrapper's process group,
    so a force-stop left the actual cooker running untouched.
    """
    run_dir = _make_run_dir(tmp_path)
    build_stop.write_launch_record(run_dir, pgid=4242, mode="host")
    (run_dir.parent.parent / "bitbake.lock").write_text("999999\n")

    monkeypatch.setattr(build_stop, "is_build_running", lambda _rd: (True, 4242, True))
    monkeypatch.setattr(build_stop, "_pgid_alive", lambda _pgid: False)
    monkeypatch.setattr(build_stop.time, "sleep", lambda _s: None)
    _record_killpg(monkeypatch)

    kill_calls: list[tuple[int, int]] = []

    def fake_kill(pid: int, sig: int) -> None:
        kill_calls.append((pid, sig))
        raise ProcessLookupError

    monkeypatch.setattr(build_stop.os, "kill", fake_kill)

    assert build_stop.stop_build(tmp_path, force=True) is True
    assert (999999, signal.SIGTERM) in kill_calls


# --- mode-aware stop_build branching ---------------------------------------


def test_stop_build_container_mode_stops_container_not_pgid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A container record resolves+stops the container and never touches the PGID."""
    run_dir = _make_run_dir(tmp_path)
    build_stop.write_launch_record(
        run_dir,
        pgid=4242,
        mode="container",
        runtime="docker",
        container_label="bakar.run_id=20260618-120000",
    )

    stop_calls: list[tuple[str, str]] = []

    # _container_id is called twice: once to discover the container to stop, and
    # once by the post-stop verify. Resolve on discovery, then report the
    # container gone so verify passes (mirroring the real docker rm -f).
    container_ids = iter(["cafef00d", None])
    monkeypatch.setattr(build_stop, "_container_id", lambda _rt, _label: next(container_ids, None))
    monkeypatch.setattr(
        build_stop,
        "_stop_container",
        lambda runtime, cid, **_kw: stop_calls.append((runtime, cid)),
    )
    monkeypatch.setattr(build_stop.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(build_stop.time, "sleep", lambda _s: None)
    calls = _record_killpg(monkeypatch)

    assert build_stop.stop_build(tmp_path) is True

    assert stop_calls == [("docker", "cafef00d")]
    assert calls == []
    assert not (run_dir / "build.pid").exists()
    assert not (run_dir / "build.meta.json").exists()


def test_stop_build_host_mode_signals_pgid_not_container(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host record signals the PGID with SIGINT and never stops a container."""
    run_dir = _make_run_dir(tmp_path)
    build_stop.write_launch_record(run_dir, pgid=4242, mode="host")

    stop_calls: list[tuple[str, str]] = []

    monkeypatch.setattr(build_stop, "is_build_running", lambda _rd: (True, 4242, True))
    monkeypatch.setattr(build_stop, "_pgid_alive", lambda _pgid: False)
    monkeypatch.setattr(
        build_stop,
        "_stop_container",
        lambda runtime, cid, **_kw: stop_calls.append((runtime, cid)),
    )
    monkeypatch.setattr(build_stop.time, "sleep", lambda _s: None)
    calls = _record_killpg(monkeypatch)

    assert build_stop.stop_build(tmp_path) is True

    assert calls == [(4242, signal.SIGINT)]
    assert stop_calls == []
    assert not (run_dir / "build.pid").exists()
    assert not (run_dir / "build.meta.json").exists()


def test_stop_build_legacy_run_targets_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A legacy run (build.pid only, no build.meta.json) cannot be targeted.

    read_launch_record classifies a bare pidfile as a container run with no
    label, so stop_build must signal neither the wrapper PGID nor a container,
    return False, and print no "stopped" success line.
    """
    run_dir = _make_run_dir(tmp_path)
    build_stop.write_pid(run_dir, 4242)  # legacy: no build.meta.json

    stop_calls: list[tuple[str, str]] = []

    monkeypatch.setattr(
        build_stop,
        "_stop_container",
        lambda runtime, cid, **_kw: stop_calls.append((runtime, cid)),
    )
    monkeypatch.setattr(build_stop.time, "sleep", lambda _s: None)
    calls = _record_killpg(monkeypatch)

    assert build_stop.stop_build(tmp_path) is False

    assert calls == []
    assert stop_calls == []
    assert "stopped" not in capsys.readouterr().out
    assert not (run_dir / "build.pid").exists()
    assert not (run_dir / "build.meta.json").exists()


def test_stop_build_container_id_unresolved_is_idempotent_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A container label resolving to no live container is a clean-tree success.

    The build already finished (or was killed) so there is nothing to stop;
    ``stop_build`` clears any stale lock/sock and returns True (exit 0) instead
    of erroring, matching the host idempotent-clean-tree path.
    """
    run_dir = _make_run_dir(tmp_path)
    build_stop.write_launch_record(
        run_dir,
        pgid=4242,
        mode="container",
        runtime="docker",
        container_label="bakar.run_id=20260618-120000",
    )

    stop_calls: list[tuple[str, str]] = []

    monkeypatch.setattr(build_stop, "_container_id", lambda _rt, _label: None)
    monkeypatch.setattr(
        build_stop,
        "_stop_container",
        lambda runtime, cid, **_kw: stop_calls.append((runtime, cid)),
    )
    monkeypatch.setattr(build_stop.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(build_stop.time, "sleep", lambda _s: None)
    calls = _record_killpg(monkeypatch)

    assert build_stop.stop_build(tmp_path) is True

    assert stop_calls == []
    assert calls == []
    assert not (run_dir / "build.pid").exists()
    assert not (run_dir / "build.meta.json").exists()


# --- check_unclean_stop -----------------------------------------------------


def test_check_unclean_stop_stale_names_interrupted_step(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale build.pid with an unmatched step_start warns and names the step."""
    run_dir = _make_run_dir(tmp_path)
    build_stop.write_pid(run_dir, 9999999)  # dead PGID -> stale
    events = run_dir / "events.jsonl"
    events.write_text(json.dumps({"event": "step_start", "step": "kas_build"}) + "\n")

    # Dead PGID: is_build_running already reports live=False for 9999999, but
    # pin it explicitly so the test does not depend on PID 9999999 being unused.
    monkeypatch.setattr(build_stop, "is_build_running", lambda _rd: (False, 9999999, False))
    console = Console(record=True, width=100)

    build_stop.check_unclean_stop(tmp_path, console)

    output = console.export_text()
    assert "interrupted uncleanly" in output
    assert "kas_build" in output


def test_interrupted_step_names_later_step_after_terminal_events(tmp_path: Path) -> None:
    """A step terminated by step_ok/step_fail/step_skip is not interrupted; a
    later step with no terminal event is."""
    run_dir = _make_run_dir(tmp_path)
    events = run_dir / "events.jsonl"
    events.write_text(
        json.dumps({"event": "step_start", "step": "sync"})
        + "\n"
        + json.dumps({"event": "step_ok", "step": "sync"})
        + "\n"
        + json.dumps({"event": "step_start", "step": "gen_kas"})
        + "\n"
        + json.dumps({"event": "step_skip", "step": "gen_kas"})
        + "\n"
        + json.dumps({"event": "step_start", "step": "kas_build"})
        + "\n"
    )

    assert build_stop._interrupted_step(run_dir) == "kas_build"


def test_interrupted_step_none_when_all_terminated(tmp_path: Path) -> None:
    """No interrupted step when every step_start has a terminal event."""
    run_dir = _make_run_dir(tmp_path)
    events = run_dir / "events.jsonl"
    events.write_text(
        json.dumps({"event": "step_start", "step": "sync"})
        + "\n"
        + json.dumps({"event": "step_fail", "step": "sync"})
        + "\n"
    )

    assert build_stop._interrupted_step(run_dir) is None


def test_check_unclean_stop_no_pidfile_silent(tmp_path: Path) -> None:
    """An empty runs dir (no build.pid) prints nothing."""
    _make_run_dir(tmp_path)  # run dir exists but holds no build.pid
    console = Console(record=True, width=100)

    build_stop.check_unclean_stop(tmp_path, console)

    assert console.export_text().strip() == ""


# --- _graceful_wait (unbounded task-aware wait) ----------------------------


def _incrementing_clock() -> object:
    """Return a clock callable that yields 0.0, 1.0, 2.0, ... on each call."""
    state = {"n": -1.0}

    def _clock() -> float:
        state["n"] += 1.0
        return state["n"]

    return _clock


def _liveness_from(statuses: list[str]) -> object:
    """Return a liveness callable that yields ``statuses`` then repeats the last."""
    it = iter(statuses)
    last = {"v": statuses[-1]}

    def _liveness() -> str:
        try:
            last["v"] = next(it)
        except StopIteration:
            pass
        return last["v"]

    return _liveness


def test_graceful_wait_long_wait_never_auto_escalates() -> None:
    """A simulated >60s wait ends on liveness=false, not tasks==0, and never escalates.

    Liveness stays alive for 70 polls (elapsed well past the old 60s cap) with an
    empty running-task set the whole time; the loop must keep waiting until the
    final _DEAD and must not fire the escalate ladder.
    """
    poll_count = {"n": 0}

    def _liveness() -> str:
        poll_count["n"] += 1
        return build_stop._DEAD if poll_count["n"] > 70 else build_stop._ALIVE

    escalate_calls: list[int] = []
    out = Console(record=True, width=100)

    status = build_stop._graceful_wait(
        liveness=_liveness,
        escalate=lambda: escalate_calls.append(1),
        target_desc="PGID 4242",
        run_dir=None,
        console_out=out,
        sleep=lambda _s: None,
        clock=_incrementing_clock(),
        tasks_reader=lambda _rd: [],
        install_signal=False,
    )

    assert status == "drained"
    assert escalate_calls == []
    assert poll_count["n"] == 71  # polled through all 70 alive iterations, ended on _DEAD


def test_graceful_wait_ends_on_liveness_not_tasks_zero() -> None:
    """With tasks==0 from the first poll, the wait still runs until liveness=false."""
    liveness = _liveness_from([build_stop._ALIVE, build_stop._ALIVE, build_stop._DEAD])
    seen: list[str] = []

    def _counting_liveness() -> str:
        v = liveness()
        seen.append(v)
        return v

    status = build_stop._graceful_wait(
        liveness=_counting_liveness,
        escalate=lambda: None,
        target_desc="PGID 1",
        run_dir=SimpleNamespace(),  # non-None so tasks_reader is consulted
        console_out=Console(record=True, width=100),
        sleep=lambda _s: None,
        clock=_incrementing_clock(),
        tasks_reader=lambda _rd: [],  # tasks==0 immediately
        install_signal=False,
    )

    assert status == "drained"
    assert seen == [build_stop._ALIVE, build_stop._ALIVE, build_stop._DEAD]


def test_graceful_wait_frozen_running_set_flips_to_spinner() -> None:
    """A running set that stops changing flips the live rows to the spinner fallback."""
    from bakar.eventlog import RunningTask

    frozen = [RunningTask(recipe="busybox", task="do_compile", started_epoch=100.0)]
    out = Console(record=True, width=120)

    status = build_stop._graceful_wait(
        liveness=_liveness_from([build_stop._ALIVE, build_stop._ALIVE, build_stop._DEAD]),
        escalate=lambda: None,
        target_desc="PGID 4242",
        run_dir=SimpleNamespace(),
        console_out=out,
        sleep=lambda _s: None,
        clock=_incrementing_clock(),
        tasks_reader=lambda _rd: frozen,
        stale_after=1.0,
        hint_interval=0.0,
        install_signal=False,
    )

    text = out.export_text()
    assert status == "drained"
    assert "busybox:do_compile" in text  # the first poll rendered a live row
    assert "press Ctrl-C to force" in text  # a later poll degraded to the spinner


def test_graceful_wait_runtime_death_cap_exits_lost_runtime() -> None:
    """A bounded run of consecutive query errors ends the wait with lost_runtime."""
    poll_count = {"n": 0}

    def _always_error() -> str:
        poll_count["n"] += 1
        return build_stop._ERROR

    escalate_calls: list[int] = []

    status = build_stop._graceful_wait(
        liveness=_always_error,
        escalate=lambda: escalate_calls.append(1),
        target_desc="container abc",
        run_dir=None,
        console_out=Console(record=True, width=100),
        error_cap=3,
        sleep=lambda _s: None,
        clock=_incrementing_clock(),
        tasks_reader=lambda _rd: [],
        install_signal=False,
    )

    assert status == "lost_runtime"
    assert poll_count["n"] == 3  # gave up after exactly error_cap consecutive errors
    assert escalate_calls == []


def test_graceful_wait_single_transient_error_keeps_waiting() -> None:
    """A single transient query error does not end the wait; the streak resets."""
    liveness = _liveness_from([build_stop._ERROR, build_stop._ALIVE, build_stop._ERROR, build_stop._DEAD])

    status = build_stop._graceful_wait(
        liveness=liveness,
        escalate=lambda: None,
        target_desc="container abc",
        run_dir=None,
        console_out=Console(record=True, width=100),
        error_cap=3,
        sleep=lambda _s: None,
        clock=_incrementing_clock(),
        tasks_reader=lambda _rd: [],
        install_signal=False,
    )

    assert status == "drained"  # never reached 3 errors in a row


def test_graceful_wait_keyboard_interrupt_runs_escalation() -> None:
    """A KeyboardInterrupt mid-wait (a Ctrl-C) fires the escalate ladder once."""
    escalate_calls: list[int] = []

    def _boom(_s: float) -> None:
        raise KeyboardInterrupt

    status = build_stop._graceful_wait(
        liveness=lambda: build_stop._ALIVE,
        escalate=lambda: escalate_calls.append(1),
        target_desc="PGID 4242",
        run_dir=None,
        console_out=Console(record=True, width=100),
        sleep=_boom,
        clock=_incrementing_clock(),
        tasks_reader=lambda _rd: [],
        install_signal=False,
    )

    assert status == "escalated"
    assert escalate_calls == [1]


def test_graceful_wait_grace_seconds_auto_escalates_without_interrupt() -> None:
    """grace_seconds > 0 auto-fires the escalate ladder once elapsed reaches it.

    No KeyboardInterrupt is ever raised - the timeout alone must trigger the
    same escalation path a Ctrl-C would, so a non-interactive caller (a script
    or an agent driving `bakar stop` through a backgrounded shell) has a way
    out of the unbounded wait without needing to signal the process itself.
    """
    escalate_calls: list[int] = []

    status = build_stop._graceful_wait(
        liveness=lambda: build_stop._ALIVE,
        escalate=lambda: escalate_calls.append(1),
        target_desc="PGID 4242",
        run_dir=None,
        console_out=Console(record=True, width=100),
        sleep=lambda _s: None,
        clock=_incrementing_clock(),
        tasks_reader=lambda _rd: [],
        install_signal=False,
        grace_seconds=5,
    )

    assert status == "escalated"
    assert escalate_calls == [1]


def test_graceful_wait_grace_seconds_zero_stays_unbounded() -> None:
    """grace_seconds=0 (the default) never auto-escalates, matching prior behavior."""
    poll_count = {"n": 0}

    def _liveness() -> str:
        poll_count["n"] += 1
        return build_stop._DEAD if poll_count["n"] > 10 else build_stop._ALIVE

    escalate_calls: list[int] = []

    status = build_stop._graceful_wait(
        liveness=_liveness,
        escalate=lambda: escalate_calls.append(1),
        target_desc="PGID 4242",
        run_dir=None,
        console_out=Console(record=True, width=100),
        sleep=lambda _s: None,
        clock=_incrementing_clock(),
        tasks_reader=lambda _rd: [],
        install_signal=False,
        grace_seconds=0,
    )

    assert status == "drained"
    assert escalate_calls == []


# --- stop_running_proc regression (unchanged in-process semantics) ---------


def test_stop_running_proc_host_sends_single_nonblocking_sigint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Host mode still sends exactly one SIGINT to the wrapper PGID and does not block."""
    calls = _record_killpg(monkeypatch)

    proc = SimpleNamespace(pid=999)
    cfg = SimpleNamespace(host_mode=True)
    log = SimpleNamespace(run_id="20260101-000000")
    build_stop.stop_running_proc(proc, cfg, log)  # type: ignore[arg-type]

    assert calls == [(999, signal.SIGINT)]


# --- _container_liveness tri-state -----------------------------------------


def test_container_liveness_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    """inspect reporting State.Running == true maps to _ALIVE."""
    monkeypatch.setattr(
        build_stop.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="true\n", stderr=""),
    )
    assert build_stop._container_liveness("docker", "cid") == build_stop._ALIVE


def test_container_liveness_dead(monkeypatch: pytest.MonkeyPatch) -> None:
    """A clean inspect reporting false is a definitive _DEAD, not an error."""
    monkeypatch.setattr(
        build_stop.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="false\n", stderr=""),
    )
    assert build_stop._container_liveness("docker", "cid") == build_stop._DEAD


def test_container_liveness_nonzero_exit_is_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-zero inspect exit is a query error (keep polling), not a drained container."""
    monkeypatch.setattr(
        build_stop.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="daemon unreachable"),
    )
    assert build_stop._container_liveness("docker", "cid") == build_stop._ERROR


def test_container_liveness_oserror_is_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """An OSError (runtime binary absent) is a query error, never _DEAD."""

    def _boom(*_a: object, **_k: object) -> SimpleNamespace:
        raise OSError("no runtime")

    monkeypatch.setattr(build_stop.subprocess, "run", _boom)
    assert build_stop._container_liveness("docker", "cid") == build_stop._ERROR


# --- _clean_stale_bitbake_files --------------------------------------------


def test_clean_stale_bitbake_files_removes_lock_and_sock(tmp_path: Path) -> None:
    """Removes bitbake.lock/bitbake.sock from TOPDIR, keeps the cookerdaemon log."""
    run_dir = _make_run_dir(tmp_path)
    topdir = run_dir.parent.parent  # <tmp>/build
    lock = topdir / "bitbake.lock"
    sock = topdir / "bitbake.sock"
    log = topdir / "bitbake-cookerdaemon.log"
    for path in (lock, sock, log):
        path.write_text("x")

    removed = build_stop._clean_stale_bitbake_files(run_dir)

    assert not lock.exists()
    assert not sock.exists()
    assert log.exists()
    assert set(removed) == {lock, sock}


def test_clean_stale_bitbake_files_absent_is_noop(tmp_path: Path) -> None:
    """Absent lock/sock files: the helper is a no-op returning [] without raising."""
    run_dir = _make_run_dir(tmp_path)

    removed = build_stop._clean_stale_bitbake_files(run_dir)

    assert removed == []


def test_clean_stale_bitbake_files_targets_topdir_not_run_dir(tmp_path: Path) -> None:
    """The helper removes files from run_dir.parent.parent, never from run_dir."""
    run_dir = _make_run_dir(tmp_path)
    topdir = run_dir.parent.parent
    (topdir / "bitbake.lock").write_text("x")
    run_dir_lock = run_dir / "bitbake.lock"
    run_dir_lock.write_text("x")

    removed = build_stop._clean_stale_bitbake_files(run_dir)

    assert not (topdir / "bitbake.lock").exists()
    assert run_dir_lock.exists()  # a lock inside the run dir is untouched
    assert removed == [topdir / "bitbake.lock"]


# --- scoped /proc reaper: _collect_build_pids ------------------------------
#
# These exercise the argv-scoped discovery that reaches a wedged, detached
# cooker (dead client fds holding the server open) the PGID and lock-PID paths
# cannot see. The /proc readers are injected so the walk is hermetic - no real
# process is ever inspected or signalled.


def _fake_proc(
    procs: dict[int, tuple[int, str]],
    pgids: dict[int, int] | None = None,
):
    """Build injectable (pids, cmdline, ppid, pgid) readers from a proc table.

    ``procs`` maps pid -> (ppid, cmdline); ``pgids`` maps pid -> pgid (defaults
    to each pid being its own group leader).
    """
    resolved_pgids = pgids if pgids is not None else {pid: pid for pid in procs}

    def _pids() -> list[int]:
        return sorted(procs)

    def _cmdline(pid: int) -> str:
        return procs.get(pid, (0, ""))[1]

    def _ppid(pid: int) -> int | None:
        return procs[pid][0] if pid in procs else None

    def _pgid(pid: int) -> int:
        if pid in resolved_pgids:
            return resolved_pgids[pid]
        raise ProcessLookupError

    return _pids, _cmdline, _ppid, _pgid


def test_collect_build_pids_matches_cooker_by_argv_path(tmp_path: Path) -> None:
    """A process whose argv references this build's bitbake.sock path is the cooker."""
    topdir = tmp_path / "build"
    sock = topdir / "bitbake.sock"
    procs = {
        4242: (1, f"python bitbake-server decafbad 7 8 {sock} idle"),
        4243: (4242, "bitbake-worker decafbad"),  # a child worker, no path in argv
        9000: (1, "python bitbake-server /other/build/bitbake.sock"),  # a DIFFERENT build
    }
    pids, cmdline, ppid, pgid = _fake_proc(procs)

    scoped = build_stop._collect_build_pids(
        topdir,
        None,
        self_pid=1,
        self_pgid=1,
        pids_reader=pids,
        cmdline_reader=cmdline,
        ppid_reader=ppid,
        pgid_reader=pgid,
    )

    assert scoped.cooker == frozenset({4242})  # only THIS build's cooker
    assert scoped.all_pids == frozenset({4242, 4243})  # cooker + its worker descendant
    assert 9000 not in scoped.all_pids  # a second build on the host is untouched


def test_collect_build_pids_includes_pgid_members_and_descendants(tmp_path: Path) -> None:
    """PGID members and their transitive children join the cooker in all_pids."""
    topdir = tmp_path / "build"
    lock = topdir / "bitbake.lock"
    procs = {
        100: (1, "kas-container build"),  # wrapper, pgid 100
        101: (100, "docker run ..."),  # wrapper child
        555: (1, f"python bitbake-server {lock}"),  # detached cooker (own session)
        556: (555, "bitbake-worker decafbad"),  # cooker's worker
    }
    pgids = {100: 100, 101: 100, 555: 555, 556: 555}
    pids, cmdline, ppid, pgid = _fake_proc(procs, pgids)

    scoped = build_stop._collect_build_pids(
        topdir,
        100,
        self_pid=1,
        self_pgid=1,
        pids_reader=pids,
        cmdline_reader=cmdline,
        ppid_reader=ppid,
        pgid_reader=pgid,
    )

    assert scoped.cooker == frozenset({555})
    assert scoped.all_pids == frozenset({100, 101, 555, 556})


def test_collect_build_pids_never_includes_self_or_own_group(tmp_path: Path) -> None:
    """The `bakar stop` process and its group are dropped even if they'd match."""
    topdir = tmp_path / "build"
    lock = topdir / "bitbake.lock"
    procs = {
        42: (1, f"bakar stop {lock}"),  # our own stop process references the path
        43: (42, "child-of-stop"),
        555: (1, f"python bitbake-server {lock}"),
    }
    pgids = {42: 42, 43: 42, 555: 555}
    pids, cmdline, ppid, pgid = _fake_proc(procs, pgids)

    scoped = build_stop._collect_build_pids(
        topdir,
        None,
        self_pid=42,
        self_pgid=42,
        pids_reader=pids,
        cmdline_reader=cmdline,
        ppid_reader=ppid,
        pgid_reader=pgid,
    )

    assert 42 not in scoped.all_pids  # never signal ourselves
    assert 43 not in scoped.all_pids  # nor our own group
    assert scoped.cooker == frozenset({555})


def test_collect_build_pids_empty_when_no_match(tmp_path: Path) -> None:
    """No argv match and no PGID members -> empty set (clean-tree short circuit)."""
    topdir = tmp_path / "build"
    procs = {10: (1, "unrelated"), 11: (1, "also unrelated")}
    pids, cmdline, ppid, pgid = _fake_proc(procs)

    scoped = build_stop._collect_build_pids(
        topdir,
        None,
        self_pid=1,
        self_pgid=1,
        pids_reader=pids,
        cmdline_reader=cmdline,
        ppid_reader=ppid,
        pgid_reader=pgid,
    )

    assert scoped.cooker == frozenset()
    assert scoped.all_pids == frozenset()


# --- _killpg guard ---------------------------------------------------------


def test_killpg_refuses_zero_and_negative(monkeypatch: pytest.MonkeyPatch) -> None:
    """_killpg refuses pgid <= 0 (would hit our own group / every process)."""
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(build_stop.os, "killpg", lambda pgid, sig: calls.append((pgid, sig)))

    assert build_stop._killpg(0, signal.SIGKILL) is False
    assert build_stop._killpg(-1, signal.SIGKILL) is False
    assert build_stop._killpg(4242, signal.SIGTERM) is True

    assert calls == [(4242, signal.SIGTERM)]  # only the valid group was signalled


# --- _report_stale_cleanup holder gate -------------------------------------


def test_report_stale_cleanup_skips_removal_when_held(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A live argv-scoped holder blocks lock/sock removal (never yank a live lock)."""
    run_dir = _make_run_dir(tmp_path)
    topdir = run_dir.parent.parent
    (topdir / "bitbake.lock").write_text("x")
    (topdir / "bitbake.sock").write_text("x")

    monkeypatch.setattr(
        build_stop,
        "_collect_build_pids",
        lambda _td, _pgid, **_kw: build_stop._ScopedProcs(cooker=frozenset({777}), all_pids=frozenset({777})),
    )

    removed = build_stop._report_stale_cleanup(run_dir)

    assert removed == []
    assert (topdir / "bitbake.lock").exists()  # left in place - a holder is alive
    assert "still held by pid(s) [777]" in capsys.readouterr().out


def test_report_stale_cleanup_removes_when_unheld(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No holder -> lock/sock are removed and returned."""
    run_dir = _make_run_dir(tmp_path)
    topdir = run_dir.parent.parent
    (topdir / "bitbake.lock").write_text("x")
    (topdir / "bitbake.sock").write_text("x")

    monkeypatch.setattr(
        build_stop,
        "_collect_build_pids",
        lambda _td, _pgid, **_kw: build_stop._ScopedProcs(cooker=frozenset(), all_pids=frozenset()),
    )

    removed = build_stop._report_stale_cleanup(run_dir)

    assert set(removed) == {topdir / "bitbake.lock", topdir / "bitbake.sock"}
    assert not (topdir / "bitbake.lock").exists()


# --- _verify_clean ---------------------------------------------------------


def test_verify_clean_reports_all_remaining(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every still-present target class becomes a reason string."""
    run_dir = _make_run_dir(tmp_path)
    topdir = run_dir.parent.parent
    (topdir / "bitbake.lock").write_text(f"{os.getpid()}\n")  # a live server pid
    (topdir / "bitbake.sock").write_text("x")

    monkeypatch.setattr(
        build_stop,
        "_collect_build_pids",
        lambda _td, _pgid, **_kw: build_stop._ScopedProcs(cooker=frozenset({321}), all_pids=frozenset({321})),
    )
    monkeypatch.setattr(build_stop, "_pgid_alive", lambda _pgid: True)
    monkeypatch.setattr(build_stop, "_container_id", lambda _rt, _label: "cid123")

    reasons = build_stop._verify_clean(run_dir, 4242, runtime="docker", container_label="bakar.run_id=X")

    joined = " ".join(reasons)
    assert "cooker/worker still running (pids [321])" in joined
    assert "process group 4242 still alive" in joined
    assert "bitbake-server (from bitbake.lock) still alive" in joined
    assert "build container still running" in joined
    assert "bitbake.lock still present" in joined
    assert "bitbake.sock still present" in joined


def test_verify_clean_empty_when_all_gone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fully-stopped build with no lock/sock verifies clean (no reasons)."""
    run_dir = _make_run_dir(tmp_path)

    monkeypatch.setattr(
        build_stop,
        "_collect_build_pids",
        lambda _td, _pgid, **_kw: build_stop._ScopedProcs(cooker=frozenset(), all_pids=frozenset()),
    )
    monkeypatch.setattr(build_stop, "_pgid_alive", lambda _pgid: False)

    assert build_stop._verify_clean(run_dir, 4242) == []


def test_stop_build_host_returns_false_when_verify_finds_survivor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a cooker survives the escalation, stop_build reports incomplete (False)."""
    run_dir = _make_run_dir(tmp_path)
    build_stop.write_launch_record(run_dir, pgid=4242, mode="host")

    monkeypatch.setattr(build_stop, "is_build_running", lambda _rd: (True, 4242, True))
    monkeypatch.setattr(build_stop, "_pgid_alive", lambda _pgid: False)
    monkeypatch.setattr(build_stop.time, "sleep", lambda _s: None)
    monkeypatch.setattr(build_stop, "_graceful_wait", lambda **_kw: "escalated")
    # Verify finds a lingering cooker the kill could not reach (e.g. EPERM).
    monkeypatch.setattr(
        build_stop,
        "_verify_clean",
        lambda *_a, **_k: ["bitbake cooker/worker still running (pids [99])"],
    )
    _record_killpg(monkeypatch)

    assert build_stop.stop_build(tmp_path) is False
    assert not (run_dir / "build.pid").exists()
