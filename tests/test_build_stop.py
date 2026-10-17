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
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from rich.console import Console

from bakar import build_stop, observability, proc_tree, report, run_discovery, stop_container, triage
from bakar.commands import stop as stop_cmd
from bakar.commands import triage as commands_triage
from tests.conftest import make_build_config

if TYPE_CHECKING:
    from collections.abc import Callable

    from bakar.config import BuildConfig

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
    monkeypatch.setattr(proc_tree, "_pgid_alive", lambda _pgid: False)
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
    monkeypatch.setattr(proc_tree, "_pgid_alive", lambda _pgid: False)
    monkeypatch.setattr(build_stop.time, "sleep", lambda _s: None)
    calls = _record_killpg(monkeypatch)

    assert build_stop.stop_build(tmp_path) is True

    assert calls == [(4242, signal.SIGINT)]
    assert not (older / "build.pid").exists()


def test_escalate_host_sigterm_then_sigkill_when_lingering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_escalate_host sends SIGTERM then SIGKILL when the PGID survives the term wait."""
    monkeypatch.setattr(proc_tree, "_pgid_alive", lambda _pgid: True)
    monkeypatch.setattr(build_stop.time, "sleep", lambda _s: None)
    calls = _record_killpg(monkeypatch)

    build_stop._escalate_host(4242)

    assert calls == [(4242, signal.SIGTERM), (4242, signal.SIGKILL)]


def test_escalate_host_no_sigkill_when_dead_after_sigterm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_escalate_host skips SIGKILL when the group is gone after SIGTERM."""
    monkeypatch.setattr(proc_tree, "_pgid_alive", lambda _pgid: False)
    monkeypatch.setattr(build_stop.time, "sleep", lambda _s: None)
    calls = _record_killpg(monkeypatch)

    build_stop._escalate_host(4242)

    assert calls == [(4242, signal.SIGTERM)]


def test_escalate_process_tree_refuses_a_child_sharing_our_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The headline falsifier for the public wrapper.

    A child spawned without ``start_new_session=True`` sits in bakar's own
    process group, so signalling its pgid would kill bakar - and, in a build
    worker, every sibling sharing that group. The wrapper must refuse rather
    than escalate.
    """
    monkeypatch.setattr(build_stop.os, "getpgid", lambda _pid: 999)
    monkeypatch.setattr(build_stop.time, "sleep", lambda _s: None)
    calls = _record_killpg(monkeypatch)

    assert build_stop.escalate_process_tree(4242) == []
    assert calls == []


def test_escalate_process_tree_escalates_a_self_led_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A child that leads its own group is escalated through the usual ladder."""
    monkeypatch.setattr(build_stop.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(proc_tree, "_pgid_alive", lambda _pgid: True)
    monkeypatch.setattr(build_stop.time, "sleep", lambda _s: None)
    calls = _record_killpg(monkeypatch)

    build_stop.escalate_process_tree(4242)

    assert calls == [(4242, signal.SIGTERM), (4242, signal.SIGKILL)]


def test_escalate_process_tree_is_quiet_when_the_child_is_already_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reaped pid makes ``getpgid`` raise; with no run_dir there is nothing
    else to check, so this is nothing to escalate."""

    def _gone(_pid: int) -> int:
        raise ProcessLookupError

    monkeypatch.setattr(build_stop.os, "getpgid", _gone)
    calls = _record_killpg(monkeypatch)

    assert build_stop.escalate_process_tree(4242) == []
    assert calls == []


def test_escalate_host_skips_the_grace_sleep_when_nothing_was_signalled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A genuine no-op (no pgid, no bb_pid, no scoped pids) must not pay the
    ``_STOP_TERM_SECONDS`` grace wait - there is nothing to wait out.

    Regression test: removing ``escalate_process_tree``'s early ``return []``
    (so run_dir-scoped detection still runs when the leader is gone) made
    every call fall through to this function, and this function used to sleep
    unconditionally between the SIGTERM and SIGKILL rungs. Deliberately does
    NOT patch ``time.sleep`` - a real 5s sleep would make this test itself
    slow, which is the failure this test exists to catch.
    """
    assert build_stop._escalate_host(None, None) == []


def test_escalate_process_tree_still_reaches_a_detached_cooker_when_the_leader_is_gone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The leader dying first must not skip the run_dir-scoped layers.

    A detached bitbake-server (double-forked into its own session) is never a
    member of the leader's process group, so the leader already being gone
    says nothing about whether that cooker - and the bitbake.lock it holds -
    is still alive. Before this fix, a bare ``getpgid`` OSError returned []
    immediately and never even looked at run_dir.
    """
    run_dir = _make_run_dir(tmp_path)
    (run_dir.parent.parent / "bitbake.lock").write_text(f"{os.getpid()}\n")

    def _gone(_pid: int) -> int:
        raise ProcessLookupError

    monkeypatch.setattr(build_stop.os, "getpgid", _gone)
    monkeypatch.setattr(proc_tree, "_bitbake_server_pid_verified", lambda _pid, _topdir: True)
    monkeypatch.setattr(build_stop.time, "sleep", lambda _s: None)
    killpg_calls = _record_killpg(monkeypatch)

    kill_calls: list[tuple[int, int]] = []

    def fake_kill(pid: int, sig: int) -> None:
        kill_calls.append((pid, sig))
        raise ProcessLookupError

    monkeypatch.setattr(build_stop.os, "kill", fake_kill)

    build_stop.escalate_process_tree(4242, run_dir)

    assert (os.getpid(), signal.SIGTERM) in kill_calls
    # A gone leader has no pgid to derive - nothing must ever reach killpg.
    assert killpg_calls == []


def test_escalate_process_tree_still_reaches_a_detached_cooker_when_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refusing to killpg a group the leader does not own must not skip the
    run_dir-scoped layers either - same reasoning as the leader-gone case."""
    run_dir = _make_run_dir(tmp_path)
    (run_dir.parent.parent / "bitbake.lock").write_text(f"{os.getpid()}\n")

    monkeypatch.setattr(build_stop.os, "getpgid", lambda _pid: 999)
    monkeypatch.setattr(proc_tree, "_bitbake_server_pid_verified", lambda _pid, _topdir: True)
    monkeypatch.setattr(build_stop.time, "sleep", lambda _s: None)
    killpg_calls = _record_killpg(monkeypatch)

    kill_calls: list[tuple[int, int]] = []

    def fake_kill(pid: int, sig: int) -> None:
        kill_calls.append((pid, sig))
        raise ProcessLookupError

    monkeypatch.setattr(build_stop.os, "kill", fake_kill)

    build_stop.escalate_process_tree(4242, run_dir)

    assert (os.getpid(), signal.SIGTERM) in kill_calls
    # The refusal exists to prevent exactly this: killpg-ing a group (999)
    # bakar does not own. It must never be reached.
    assert killpg_calls == []


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
    monkeypatch.setattr(proc_tree, "_pgid_alive", _stateful_pgid_alive)
    monkeypatch.setattr(build_stop.time, "sleep", lambda _s: None)

    def _fake_wait(*, ctx: build_stop._WaitCtx) -> str:
        ctx.escalate()
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
    monkeypatch.setattr(proc_tree, "_pgid_alive", lambda _pgid: False)
    monkeypatch.setattr(build_stop.time, "sleep", lambda _s: None)
    _record_killpg(monkeypatch)

    captured: dict[str, object] = {}

    def _fake_wait(*, ctx: build_stop._WaitCtx) -> str:
        captured["grace_seconds"] = ctx.grace_seconds
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
    monkeypatch.setattr(proc_tree, "_pgid_alive", lambda _pgid: False)
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
    monkeypatch.setattr(proc_tree, "_pgid_alive", lambda _pgid: False)
    monkeypatch.setattr(build_stop.time, "sleep", lambda _s: None)
    monkeypatch.setattr(proc_tree, "_bitbake_server_pid_verified", lambda _pid, _topdir: True)
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

    def _fake_wait(*, ctx: build_stop._WaitCtx) -> str:
        observed.append(ctx.liveness())
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
    so a force-stop left the actual cooker running untouched. The PID is
    stubbed as verified here - _bitbake_server_pid_verified is exercised on
    its own below, for both the matching and the stale/recycled case.
    """
    run_dir = _make_run_dir(tmp_path)
    build_stop.write_launch_record(run_dir, pgid=4242, mode="host")
    (run_dir.parent.parent / "bitbake.lock").write_text("999999\n")

    monkeypatch.setattr(build_stop, "is_build_running", lambda _rd: (True, 4242, True))
    monkeypatch.setattr(proc_tree, "_pgid_alive", lambda _pgid: False)
    monkeypatch.setattr(build_stop.time, "sleep", lambda _s: None)
    monkeypatch.setattr(proc_tree, "_bitbake_server_pid_verified", lambda _pid, _topdir: True)
    _record_killpg(monkeypatch)

    kill_calls: list[tuple[int, int]] = []

    def fake_kill(pid: int, sig: int) -> None:
        kill_calls.append((pid, sig))
        raise ProcessLookupError

    monkeypatch.setattr(build_stop.os, "kill", fake_kill)

    assert build_stop.stop_build(tmp_path, force=True) is True
    assert (999999, signal.SIGTERM) in kill_calls


def test_escalate_host_does_not_signal_a_stale_recycled_bitbake_server_pid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bitbake.lock PID whose live cmdline no longer matches this build must
    never be signalled - the kernel may have recycled it onto an unrelated,
    but still LIVE, process in the gap between the lock read and the kill."""
    run_dir = _make_run_dir(tmp_path)
    build_stop.write_launch_record(run_dir, pgid=4242, mode="host")
    (run_dir.parent.parent / "bitbake.lock").write_text("999999\n")

    monkeypatch.setattr(build_stop, "is_build_running", lambda _rd: (True, 4242, True))
    monkeypatch.setattr(proc_tree, "_pgid_alive", lambda _pgid: False)
    monkeypatch.setattr(build_stop.time, "sleep", lambda _s: None)
    monkeypatch.setattr(proc_tree, "_proc_cmdline", lambda _pid: "/usr/bin/some-unrelated-daemon")
    _record_killpg(monkeypatch)

    kill_calls: list[tuple[int, int]] = []

    def fake_kill(pid: int, sig: int) -> None:
        kill_calls.append((pid, sig))
        if pid == 999999 and sig == 0:
            return  # the recycled pid IS alive - it is simply not ours
        raise ProcessLookupError

    monkeypatch.setattr(build_stop.os, "kill", fake_kill)

    assert build_stop.stop_build(tmp_path, force=True) is True
    assert (999999, signal.SIGTERM) not in kill_calls
    assert (999999, signal.SIGKILL) not in kill_calls


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

    # _container_id_status is called twice: once to discover the container to
    # stop, and once by the post-stop verify. Resolve on discovery, then
    # report the container gone so verify passes (mirroring the real
    # docker rm -f).
    container_ids = iter([(build_stop._ALIVE, "cafef00d"), (build_stop._DEAD, None)])
    monkeypatch.setattr(
        stop_container, "_container_id_status", lambda _rt, _label: next(container_ids, (build_stop._DEAD, None))
    )
    monkeypatch.setattr(
        build_stop,
        "_stop_container",
        lambda runtime, cid, _plan: stop_calls.append((runtime, cid)),
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
    monkeypatch.setattr(proc_tree, "_pgid_alive", lambda _pgid: False)
    monkeypatch.setattr(
        build_stop,
        "_stop_container",
        lambda runtime, cid, _plan: stop_calls.append((runtime, cid)),
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
        lambda runtime, cid, _plan: stop_calls.append((runtime, cid)),
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

    monkeypatch.setattr(stop_container, "_container_id_status", lambda _rt, _label: (build_stop._DEAD, None))
    monkeypatch.setattr(
        build_stop,
        "_stop_container",
        lambda runtime, cid, _plan: stop_calls.append((runtime, cid)),
    )
    monkeypatch.setattr(build_stop.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(build_stop.time, "sleep", lambda _s: None)
    calls = _record_killpg(monkeypatch)

    assert build_stop.stop_build(tmp_path) is True

    assert stop_calls == []
    assert calls == []
    assert not (run_dir / "build.pid").exists()
    assert not (run_dir / "build.meta.json").exists()


def test_stop_build_container_query_error_refuses_not_idempotent_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A runtime query error is not the same as a confirmed-dead container.

    ``live_workspace_runs`` treats an unanswerable runtime query as "still
    live" (fail conservative) rather than dropping the candidate. ``stop_run``
    must not turn around and treat that same ambiguity as "confirmed dead":
    collapsing an _ERROR into the idempotent clean-tree success path would
    report a stop that never happened and delete the only tracking record of
    a build that may still be running. The refusal itself must not delete the
    launch record either - unlike the "run predates container tracking" and
    "runtime not installed" refusals, this record still names a real runtime
    and container_label worth preserving for a retry.
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

    monkeypatch.setattr(stop_container, "_container_id_status", lambda _rt, _label: (build_stop._ERROR, None))
    monkeypatch.setattr(
        build_stop,
        "_stop_container",
        lambda runtime, cid, _plan: stop_calls.append((runtime, cid)),
    )
    monkeypatch.setattr(build_stop.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(build_stop.time, "sleep", lambda _s: None)
    calls = _record_killpg(monkeypatch)

    assert build_stop.stop_build(tmp_path) is False

    assert stop_calls == []
    assert calls == []
    assert (run_dir / "build.pid").exists()
    assert (run_dir / "build.meta.json").exists()


def test_stop_build_container_lost_runtime_preserves_launch_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Losing contact with the runtime mid-stop must not delete the launch record.

    ``_stop_container`` returning "lost_runtime" means the container's actual
    state is unknown - it may still be running. Wiping the launch record here
    would be the same data loss the pre-check _ERROR refusal exists to avoid.
    """
    run_dir = _make_run_dir(tmp_path)
    build_stop.write_launch_record(
        run_dir,
        pgid=4242,
        mode="container",
        runtime="docker",
        container_label="bakar.run_id=20260618-120000",
    )

    monkeypatch.setattr(stop_container, "_container_id_status", lambda _rt, _label: (build_stop._ALIVE, "cafef00d"))
    monkeypatch.setattr(build_stop, "_stop_container", lambda *_a, **_kw: "lost_runtime")
    monkeypatch.setattr(build_stop.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(build_stop.time, "sleep", lambda _s: None)
    calls = _record_killpg(monkeypatch)

    assert build_stop.stop_build(tmp_path) is False

    assert calls == []
    assert (run_dir / "build.pid").exists()
    assert (run_dir / "build.meta.json").exists()


def test_stop_build_container_verify_survivor_preserves_launch_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A container that survives the stop ladder must not lose its launch record.

    ``_verify_clean`` reporting "build container still running" after a stop
    attempt means the container is still there (or its state could not be
    confirmed); ``stop_run`` must report the incomplete stop without
    destroying the record a later attempt needs to retarget it.
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
    # First call resolves the container to stop; second call (post-stop
    # verify, inside _verify_clean) reports it survived the ladder.
    container_ids = iter([(build_stop._ALIVE, "cafef00d"), (build_stop._ALIVE, "cafef00d")])
    monkeypatch.setattr(
        stop_container,
        "_container_id_status",
        lambda _rt, _label: next(container_ids, (build_stop._ALIVE, "cafef00d")),
    )
    monkeypatch.setattr(
        build_stop,
        "_stop_container",
        lambda runtime, cid, _plan: stop_calls.append((runtime, cid)),
    )
    monkeypatch.setattr(build_stop.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(build_stop.time, "sleep", lambda _s: None)
    calls = _record_killpg(monkeypatch)

    assert build_stop.stop_build(tmp_path) is False

    assert stop_calls == [("docker", "cafef00d")]
    assert calls == []
    assert (run_dir / "build.pid").exists()
    assert (run_dir / "build.meta.json").exists()


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


# --- truncated / corrupt events.jsonl ---------------------------------------


def _write_interrupted_events(run_dir: Path) -> None:
    """Write an ``events.jsonl`` shaped like a build killed mid-write.

    Every line but the last is intact; the last is a half-written JSON
    fragment, which is what a SIGKILL between ``write`` and the trailing
    newline leaves behind. The prefix records a completed ``kas_build``, a
    failed ``cve`` step, and an ``sbom`` step that never terminated - so each
    consumer of this log has a distinct, non-trivial answer to give.
    """
    lines = [
        json.dumps({"event": "run_start", "ts": "2026-06-18T12:00:00"}),
        json.dumps({"event": "step_start", "step": "sync"}),
        json.dumps({"event": "step_ok", "step": "sync"}),
        json.dumps({"event": "step_start", "step": "kas_build"}),
        json.dumps({"event": "step_ok", "step": "kas_build", "deploy_dir": "/work/build/deploy"}),
        json.dumps({"event": "step_start", "step": "cve"}),
        json.dumps({"event": "step_fail", "step": "cve", "reason": "cve db unavailable"}),
        json.dumps({"event": "step_start", "step": "sbom"}),
    ]
    truncated = '{"event": "step_ok", "step": "sb'
    (run_dir / "events.jsonl").write_text("\n".join(lines) + "\n" + truncated)


def test_interrupted_step_named_despite_truncated_final_line(tmp_path: Path) -> None:
    """A half-written final line does not hide the step from the intact prefix.

    This is the regression this whole consolidation exists for: the reader runs
    on the interrupted-build path, which is exactly when the log ends
    mid-record. Aborting on that line reports "no interrupted step" for the one
    case the lookup answers.
    """
    run_dir = _make_run_dir(tmp_path)
    _write_interrupted_events(run_dir)

    assert build_stop._interrupted_step(run_dir) == "sbom"


def test_truncated_final_line_skipped_not_fatal(tmp_path: Path) -> None:
    """The shared reader drops exactly the truncated line and keeps the rest."""
    run_dir = _make_run_dir(tmp_path)
    _write_interrupted_events(run_dir)

    stats = observability.RunEventStats()
    records = list(observability.iter_run_events(run_dir / "events.jsonl", stats=stats))

    assert stats.skipped == 1
    assert [rec["event"] for rec in records] == [
        "run_start",
        "step_start",
        "step_ok",
        "step_start",
        "step_ok",
        "step_start",
        "step_fail",
        "step_start",
    ]


def test_all_consumers_agree_on_truncated_log(tmp_path: Path) -> None:
    """The four ``events.jsonl`` consumers read the same intact prefix.

    One policy now serves all of them, so none may diverge on a log whose only
    defect is a truncated tail. Each assertion below names a value that only a
    skipping reader can produce - an aborting one yields ``None``/``failure``
    for every one of them.
    """
    run_dir = _make_run_dir(tmp_path)
    _write_interrupted_events(run_dir)
    cfg = make_build_config(workspace=tmp_path)

    assert build_stop._interrupted_step(run_dir) == "sbom"

    summary = report.assemble_report(run_dir, cfg)
    assert summary.status == "success"
    assert summary.deploy_dir == "/work/build/deploy"

    assert commands_triage._run_has_failure(run_dir) is True

    triaged = triage.analyse(run_dir, tmp_path)
    assert triaged.failing_step == "cve"
    assert triaged.fail_reason == "cve db unavailable"


def test_wholly_corrupt_log_distinguishable_from_empty(tmp_path: Path) -> None:
    """A log where nothing parses reports skips; an empty or absent one does not.

    Skipping is what keeps a truncated tail readable, and the cost is that a
    wholly corrupt log otherwise looks identical to one with no events at all.
    ``RunEventStats.skipped`` is the escape hatch that keeps those apart.
    """
    corrupt_dir = _make_run_dir(tmp_path, "20260618-130000")
    (corrupt_dir / "events.jsonl").write_text('{"event": "step_st\nnot json at all\n[1, 2, 3]\n')

    corrupt_stats = observability.RunEventStats()
    corrupt_events = list(observability.iter_run_events(corrupt_dir / "events.jsonl", stats=corrupt_stats))
    assert corrupt_events == []
    assert corrupt_stats.skipped == 3
    assert build_stop._interrupted_step(corrupt_dir) is None

    empty_dir = _make_run_dir(tmp_path, "20260618-140000")
    (empty_dir / "events.jsonl").write_text("")

    empty_stats = observability.RunEventStats()
    assert list(observability.iter_run_events(empty_dir / "events.jsonl", stats=empty_stats)) == []
    assert empty_stats.skipped == 0

    absent_dir = _make_run_dir(tmp_path, "20260618-150000")
    absent_stats = observability.RunEventStats()
    assert list(observability.iter_run_events(absent_dir / "events.jsonl", stats=absent_stats)) == []
    assert absent_stats.skipped == 0


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
        ctx=build_stop._WaitCtx(
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
        ctx=build_stop._WaitCtx(
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
    )

    assert status == "drained"
    assert seen == [build_stop._ALIVE, build_stop._ALIVE, build_stop._DEAD]


def test_graceful_wait_frozen_running_set_flips_to_spinner() -> None:
    """A running set that stops changing flips the live rows to the spinner fallback."""
    from bakar.eventlog import RunningTask

    frozen = [RunningTask(recipe="busybox", task="do_compile", started_epoch=100.0)]
    out = Console(record=True, width=120)

    status = build_stop._graceful_wait(
        ctx=build_stop._WaitCtx(
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
        ctx=build_stop._WaitCtx(
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
    )

    assert status == "lost_runtime"
    assert poll_count["n"] == 3  # gave up after exactly error_cap consecutive errors
    assert escalate_calls == []


def test_graceful_wait_single_transient_error_keeps_waiting() -> None:
    """A single transient query error does not end the wait; the streak resets."""
    liveness = _liveness_from([build_stop._ERROR, build_stop._ALIVE, build_stop._ERROR, build_stop._DEAD])

    status = build_stop._graceful_wait(
        ctx=build_stop._WaitCtx(
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
    )

    assert status == "drained"  # never reached 3 errors in a row


def test_graceful_wait_keyboard_interrupt_runs_escalation() -> None:
    """A KeyboardInterrupt mid-wait (a Ctrl-C) fires the escalate ladder once."""
    escalate_calls: list[int] = []

    def _boom(_s: float) -> None:
        raise KeyboardInterrupt

    status = build_stop._graceful_wait(
        ctx=build_stop._WaitCtx(
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
        ctx=build_stop._WaitCtx(
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
        ctx=build_stop._WaitCtx(
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
    )

    assert status == "drained"
    assert escalate_calls == []


# --- _WaitCtx value preservation at the _stop_container call site ----------


def _capture_wait_ctx(monkeypatch: pytest.MonkeyPatch, status: str = "drained") -> list[build_stop._WaitCtx]:
    """Stub ``_graceful_wait`` so the ctx its caller built can be inspected."""
    captured: list[build_stop._WaitCtx] = []

    def _fake_wait(*, ctx: build_stop._WaitCtx) -> str:
        captured.append(ctx)
        return status

    monkeypatch.setattr(build_stop, "_graceful_wait", _fake_wait)
    monkeypatch.setattr(stop_container, "_sigint_bitbake_in_container", lambda _rt, _cid: True)
    return captured


def test_stop_container_wait_ctx_carries_every_field_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every _WaitCtx field equals what ``_stop_container`` was called with.

    Packing fourteen keyword-only parameters into a context is wrong in a way
    the rest of this file cannot see: transpose two fields or drop one and the
    stop still runs and every other test stays green, because nothing observes
    the packed values. This captures the context and compares it field-by-field
    against a call where no argument is the default.
    """
    import dataclasses

    run_dir = _make_run_dir(tmp_path)
    out = Console(record=True, width=100)
    captured = _capture_wait_ctx(monkeypatch)

    liveness_calls: list[tuple[str, str]] = []
    escalate_calls: list[tuple[str, str, int]] = []

    def _record_liveness(runtime: str, cid: str) -> str:
        liveness_calls.append((runtime, cid))
        return build_stop._ALIVE

    monkeypatch.setattr(stop_container, "_container_liveness", _record_liveness)
    monkeypatch.setattr(
        stop_container,
        "_escalate_container",
        lambda rt, cid, secs: escalate_calls.append((rt, cid, secs)),
    )

    build_stop._stop_container(
        "podman",
        "cid123",
        build_stop._ContainerStopPlan(
            force=False,
            term_secs=7,
            run_dir=run_dir,
            console_out=out,
            grace_seconds=45,
        ),
    )

    assert len(captured) == 1, f"expected one ctx, got {len(captured)}"
    ctx = captured[0]

    # The two callables are compared by what they do, not by identity.
    assert ctx.liveness() == build_stop._ALIVE
    assert liveness_calls == [("podman", "cid123")]
    ctx.escalate()
    assert escalate_calls == [("podman", "cid123", 7)]

    expected = {
        "target_desc": "container cid123",
        "run_dir": run_dir,
        "console_out": out,
        "grace_seconds": 45,
        # Fields _stop_container leaves alone must still hold their defaults;
        # a transposition that overwrites one of these is caught here.
        "error_cap": build_stop._RUNTIME_ERROR_CAP,
        "sleep": build_stop.time.sleep,
        "clock": build_stop.time.monotonic,
        "tasks_reader": None,
        "poll_interval": build_stop._STOP_POLL_SECONDS,
        "stale_after": build_stop._STOP_STALE_SECONDS,
        "hint_interval": build_stop._STOP_HINT_SECONDS,
        "install_signal": True,
    }
    for field, want in expected.items():
        assert getattr(ctx, field) == want, f"_WaitCtx.{field}: expected {want!r}, got {getattr(ctx, field)!r}"

    # Guards against a field being added to the dataclass but left unasserted.
    asserted = set(expected) | {"liveness", "escalate"}
    assert {f.name for f in dataclasses.fields(ctx)} == asserted


@pytest.mark.parametrize(
    "case",
    [("run_dir", "console_out"), ("console_out", "run_dir")],
    ids=lambda c: c[0],
)
def test_stop_container_wait_ctx_sets_only_the_none_field_passed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: tuple[str, str],
) -> None:
    """One None-defaulting field at a time: it moves and its neighbour does not.

    ``run_dir`` and ``console_out`` share the ``None`` default, so the
    all-fields test above gives both a non-None value and a transposition
    between them survives it. Passing exactly one per case makes the values
    differ, which is what catches a swapped pair.
    """
    import dataclasses

    field, neighbour = case
    none_fields = [f.name for f in dataclasses.fields(build_stop._WaitCtx) if f.default is None]
    assert field in none_fields, f"{field} is not a None-defaulting field of _WaitCtx"
    assert neighbour in none_fields, f"{neighbour} is not a None-defaulting field of _WaitCtx"

    values = {
        "run_dir": _make_run_dir(tmp_path),
        "console_out": Console(record=True, width=100),
    }
    captured = _capture_wait_ctx(monkeypatch)

    build_stop._stop_container(
        "podman", "cid123", build_stop._ContainerStopPlan(force=False, term_secs=7, **{field: values[field]})
    )

    assert len(captured) == 1, f"expected one ctx, got {len(captured)}"
    ctx = captured[0]
    assert getattr(ctx, field) is values[field], f"passing {field} should reach _WaitCtx.{field}"
    assert getattr(ctx, neighbour) is None, (
        f"passing only {field} left _WaitCtx.{neighbour} as {getattr(ctx, neighbour)!r}"
    )


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
        proc_tree,
        "_collect_build_pids",
        lambda _td, _pgid, **_kw: proc_tree._ScopedProcs(cooker=frozenset({777}), all_pids=frozenset({777})),
    )

    removed = build_stop._report_stale_cleanup(run_dir)

    assert removed == []
    assert (topdir / "bitbake.lock").exists()  # left in place - a holder is alive
    assert "still held by pid(s) [777]" in capsys.readouterr().err


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
        proc_tree,
        "_collect_build_pids",
        lambda _td, _pgid, **_kw: proc_tree._ScopedProcs(cooker=frozenset(), all_pids=frozenset()),
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
        proc_tree,
        "_collect_build_pids",
        lambda _td, _pgid, **_kw: proc_tree._ScopedProcs(cooker=frozenset({321}), all_pids=frozenset({321})),
    )
    monkeypatch.setattr(proc_tree, "_pgid_alive", lambda _pgid: True)
    monkeypatch.setattr(stop_container, "_container_id_status", lambda _rt, _label: (build_stop._ALIVE, "cid123"))
    monkeypatch.setattr(proc_tree, "_bitbake_server_pid_verified", lambda _pid, _topdir: True)

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
        proc_tree,
        "_collect_build_pids",
        lambda _td, _pgid, **_kw: proc_tree._ScopedProcs(cooker=frozenset(), all_pids=frozenset()),
    )
    monkeypatch.setattr(proc_tree, "_pgid_alive", lambda _pgid: False)

    assert build_stop._verify_clean(run_dir, 4242) == []


def test_stop_build_host_returns_false_when_verify_finds_survivor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a cooker survives the escalation, stop_build reports incomplete (False)."""
    run_dir = _make_run_dir(tmp_path)
    build_stop.write_launch_record(run_dir, pgid=4242, mode="host")

    monkeypatch.setattr(build_stop, "is_build_running", lambda _rd: (True, 4242, True))
    monkeypatch.setattr(proc_tree, "_pgid_alive", lambda _pgid: False)
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


# --- lock-ownership gate -----------------------------------------------


def _cfg_with_build_dir(tmp_path: Path) -> tuple[BuildConfig, Path]:
    """An NXP BuildConfig whose TOPDIR (bsp_root/build_dir_name) exists on disk."""
    cfg = make_build_config(workspace=tmp_path)
    build_dir = cfg.bsp_root / cfg.build_dir_name
    build_dir.mkdir(parents=True, exist_ok=True)
    return cfg, build_dir


def test_lock_marker_path_uses_build_dir_name(tmp_path: Path) -> None:
    """lock_marker_path is rooted at bsp_root/build_dir_name, not a hardcoded 'build'."""
    cfg, build_dir = _cfg_with_build_dir(tmp_path)

    assert build_stop.lock_marker_path(cfg) == build_dir / ".bakar-lock-host"


def test_read_marker_owner_none_when_absent(tmp_path: Path) -> None:
    """No marker file at all -> None (never a peer)."""
    cfg, _build_dir = _cfg_with_build_dir(tmp_path)

    assert build_stop.read_marker_owner(cfg) is None


def test_read_marker_owner_none_when_empty(tmp_path: Path) -> None:
    """A whitespace-only marker (e.g. a truncated write) -> None."""
    cfg, build_dir = _cfg_with_build_dir(tmp_path)
    (build_dir / ".bakar-lock-host").write_text("   \n")

    assert build_stop.read_marker_owner(cfg) is None


def test_read_marker_owner_none_when_torn_multiline(tmp_path: Path) -> None:
    """A torn/concurrent write leaving two hostnames on separate lines is garbled, never a peer."""
    cfg, build_dir = _cfg_with_build_dir(tmp_path)
    (build_dir / ".bakar-lock-host").write_text("hosta\nhostb\n")

    assert build_stop.read_marker_owner(cfg) is None


def test_read_marker_owner_returns_stripped_hostname(tmp_path: Path) -> None:
    """A well-formed marker returns the stripped hostname."""
    cfg, build_dir = _cfg_with_build_dir(tmp_path)
    (build_dir / ".bakar-lock-host").write_text("  some-host  \n")

    assert build_stop.read_marker_owner(cfg) == "some-host"


def test_lock_mutation_guard_peer_held(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A marker naming another host refuses as peer-held, regardless of lock/fs state."""
    cfg, build_dir = _cfg_with_build_dir(tmp_path)
    (build_dir / ".bakar-lock-host").write_text("peer-host\n")
    monkeypatch.setattr(build_stop.socket, "gethostname", lambda: "this-host")

    refusal = build_stop.lock_mutation_guard(cfg)

    assert refusal is not None
    assert refusal.reason == "peer-held"
    assert refusal.host == "peer-host"


def test_lock_mutation_guard_local_marker_is_safe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A marker naming this host is unconditionally safe (None), no fs/lock check needed."""
    cfg, build_dir = _cfg_with_build_dir(tmp_path)
    (build_dir / ".bakar-lock-host").write_text("this-host\n")
    monkeypatch.setattr(build_stop.socket, "gethostname", lambda: "this-host")

    assert build_stop.lock_mutation_guard(cfg) is None


def test_lock_mutation_guard_garbled_marker_never_classifies_as_peer_held(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    nfs_probe: Callable[[bool | None], None],
) -> None:
    """A garbled (torn multiline) marker must never classify as peer-held."""
    cfg, build_dir = _cfg_with_build_dir(tmp_path)
    (build_dir / ".bakar-lock-host").write_text("hosta\nhostb\n")
    monkeypatch.setattr(build_stop.socket, "gethostname", lambda: "this-host")
    nfs_probe(False)

    refusal = build_stop.lock_mutation_guard(cfg)

    assert refusal is None or refusal.reason != "peer-held"


def test_lock_mutation_guard_unattributable_lock_present_shared_fs(
    tmp_path: Path,
    nfs_probe: Callable[[bool | None], None],
) -> None:
    """No reliable owner + lock PRESENT + shared/unknown fs -> unattributable."""
    cfg, build_dir = _cfg_with_build_dir(tmp_path)
    (build_dir / "bitbake.lock").write_text("4242\n")
    nfs_probe(True)

    refusal = build_stop.lock_mutation_guard(cfg)

    assert refusal is not None
    assert refusal.reason == "unattributable"


def test_lock_mutation_guard_shared_inaction_lock_absent_shared_fs(
    tmp_path: Path,
    nfs_probe: Callable[[bool | None], None],
) -> None:
    """No reliable owner + lock ABSENT + shared/unknown fs -> shared-inaction, not a bare None.

    A bare None here would let a caller run a blind unconditional unlink on the
    strength of an absence view that NFS negative-lookup caching can make stale
    for up to ~60s after a peer creates the lock.
    """
    cfg, _build_dir = _cfg_with_build_dir(tmp_path)
    nfs_probe(None)

    refusal = build_stop.lock_mutation_guard(cfg)

    assert refusal is not None
    assert refusal.reason == "shared-inaction"


def test_lock_mutation_guard_none_when_confirmed_local(
    tmp_path: Path, nfs_probe: Callable[[bool | None], None]
) -> None:
    """No reliable owner but the filesystem is CONFIRMED local -> safe (None)."""
    cfg, build_dir = _cfg_with_build_dir(tmp_path)
    (build_dir / "bitbake.lock").write_text("4242\n")
    nfs_probe(False)

    assert build_stop.lock_mutation_guard(cfg) is None


# Verbatim PC3 `/proc/mounts` autofs-only fixture line: an idle systemd automount
# trap with no NFS share mounted over it yet (see tests/test_mounts_stack.py,
# task 1.1, for the stacked autofs+nfs4 companion line and its provenance).
_PC3_AUTOFS_LINE = (
    "systemd-1 /home/tiamarin/yocto-cache/ccache autofs "
    "rw,relatime,fd=69,pgrp=1,timeout=600,minproto=5,maxproto=5,direct,pipe_ino=10747 0 0"
)
_PC3_CCACHE_MOUNTPOINT = "/home/tiamarin/yocto-cache/ccache"


def _patch_proc_mounts(monkeypatch: pytest.MonkeyPatch, content: str) -> None:
    """Patch ``Path.read_text`` so reads of ``/proc/mounts`` return ``content``."""
    real_read_text = Path.read_text

    def fake_read_text(self: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if str(self) == "/proc/mounts":
            return content
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read_text)


def _patch_resolve_maps_build_dir_to_pc3_mountpoint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, build_dir: Path
) -> Path:
    """Make the bounded symlink resolution read ``build_dir`` (and any path
    under it) as living under the literal PC3 mountpoint text, without
    touching this developer's real ``~/yocto-cache`` directory (a real
    symlink on this dev machine - see the prior round's gotcha in this
    change's devspec log).

    ``is_path_on_nfs`` no longer ever calls ``.resolve()`` to find which
    mount-table entry covers a path - it forks a real ``realpath`` child
    instead (``bakar.mounts._resolve_bounded``), bounded so a wedged automount
    ancestor cannot hang the caller (bug 1's fix: the prior lexical-only
    normalization silently dropped symlink resolution outright). A patch
    aimed at ``Path.resolve`` no longer reaches anything this classifier
    calls, so this fixture replaces the ``realpath`` binary on ``PATH``
    instead. Every invocation appends one line to the returned call-log file,
    so a caller can assert the bounded-resolution path was genuinely
    exercised rather than silently skipped.

    Every other operation in this test (``.exists()``, ``.write_text()``)
    still addresses the real ``tmp_path``-backed ``build_dir``, so the
    fixture's literal mountpoint string never has to exist on disk.
    """
    build_dir_str = str(build_dir)
    bin_dir = tmp_path / "realpath-stub-bin"
    bin_dir.mkdir(exist_ok=True)
    call_log = tmp_path / "realpath-calls.log"
    stub = bin_dir / "realpath"
    stub.write_text(
        "#!/bin/sh\n"
        f'echo call >> "{call_log}"\n'
        "shift\n"
        'p="$1"\n'
        'case "$p" in\n'
        f"  {build_dir_str})\n"
        f"    printf '%s\\n' '{_PC3_CCACHE_MOUNTPOINT}'\n"
        "    ;;\n"
        f"  {build_dir_str}/*)\n"
        f"    printf '%s\\n' \"{_PC3_CCACHE_MOUNTPOINT}${{p#{build_dir_str}/}}\"\n"
        "    ;;\n"
        "  *)\n"
        '    exec /usr/bin/realpath -- "$p"\n'
        "    ;;\n"
        "esac\n"
    )
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return call_log


def test_lock_mutation_guard_idle_autofs_only_is_unattributable_with_lock_present(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An idle automount trap (no NFS share mounted over it yet) must classify
    exactly like a confirmed-shared filesystem, not like confirmed-local.

    Regression guard for the spec requirement "an automounted build directory
    is never classified as confirmed local": ``is_path_on_nfs`` returns
    ``None`` for a bare autofs entry (task 1.1), and this guard's
    ``shared_or_unknown = nfs is not False`` must still fail closed on that
    ``None`` exactly as it does on ``True`` - no ownership marker, no live PID,
    lock present -> unattributable, lock left untouched.
    """
    cfg, build_dir = _cfg_with_build_dir(tmp_path)
    lock = build_dir / "bitbake.lock"
    lock.write_text("4242\n")
    _patch_proc_mounts(monkeypatch, f"{_PC3_AUTOFS_LINE}\n")
    call_log = _patch_resolve_maps_build_dir_to_pc3_mountpoint(monkeypatch, tmp_path, build_dir)

    refusal = build_stop.lock_mutation_guard(cfg)

    assert refusal is not None
    assert refusal.reason == "unattributable"
    assert lock.exists()
    assert call_log.exists() and call_log.read_text().strip(), (
        "the bounded realpath resolution was never invoked - this test no longer exercises "
        "the real symlink-aware classification path"
    )


def test_stale_bitbake_files_includes_hashserve_sock() -> None:
    """The gate-owned stale-file set includes hashserve.sock (parity with kas_build._remove_all)."""
    assert "hashserve.sock" in build_stop._STALE_BITBAKE_FILES


def test_import_bakar_diagnostics_no_import_cycle() -> None:
    """import bakar.diagnostics must succeed: build_stop must not import it at module level."""
    result = subprocess.run(
        [sys.executable, "-c", "import bakar.diagnostics"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_import_bakar_build_stop_no_import_cycle() -> None:
    """import bakar.build_stop must also succeed standalone."""
    result = subprocess.run(
        [sys.executable, "-c", "import bakar.build_stop"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


# --- bakar stop gates PID trust, not just files (task 3.1) -----------------


def _make_run_dir_for_cfg(cfg: BuildConfig, run_id: str = "20260618-120000") -> Path:
    """Create ``cfg.bsp_root/cfg.build_dir_name/runs/<run_id>`` and return it."""
    run_dir = cfg.bsp_root / cfg.build_dir_name / "runs" / run_id
    run_dir.mkdir(parents=True)
    return run_dir


def _record_kill_pid(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int]]:
    """Patch proc_tree._kill_pid to record (pid, sig) calls instead of signalling."""
    calls: list[tuple[int, int]] = []

    def fake_kill_pid(pid: int, sig: int) -> bool:
        calls.append((pid, sig))
        return True

    monkeypatch.setattr(proc_tree, "_kill_pid", fake_kill_pid)
    return calls


def _forbid_wait_and_escalate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if the graceful-wait/escalation ladder is ever reached.

    A peer-held/unattributable/shared-inaction refusal must return before
    ``stop_build`` gets anywhere near ``_graceful_wait``/``_escalate_host`` - if
    either fires, the gate did not actually short-circuit the stop.
    """

    def _fail_wait(**_kwargs: object) -> str:
        pytest.fail("_graceful_wait must not be reached when the ownership gate refuses")

    def _fail_escalate(*_args: object, **_kwargs: object) -> list[object]:
        pytest.fail("_escalate_host must not be reached when the ownership gate refuses")

    monkeypatch.setattr(build_stop, "_graceful_wait", _fail_wait)
    monkeypatch.setattr(build_stop, "_escalate_host", _fail_escalate)


@pytest.mark.parametrize("reason", ["peer-held", "unattributable", "shared-inaction"])
def test_stop_build_sends_zero_signals_on_any_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    reason: str,
) -> None:
    """Any hard-refusing guard verdict sends ZERO signals and touches nothing.

    The falsifier for this task is any SIGINT/SIGTERM/SIGKILL reaching
    ``_kill_pid``/``os.killpg`` when the marker names a peer, ownership is
    unattributable, or the lock reads absent on a shared/unverifiable
    filesystem (``shared-inaction``) - all three must refuse the whole stop
    before any signal is sent, and before the run-dir scan even starts.
    ``shared-inaction`` gets no special fall-through: the node-local scan
    that would otherwise run has no way to distinguish a genuinely idle
    workspace from a peer's live build hidden by NFS negative-lookup
    caching, so it refuses just as hard as the other two verdicts.
    """
    cfg = make_build_config(workspace=tmp_path)
    run_dir = _make_run_dir_for_cfg(cfg)
    build_stop.write_launch_record(run_dir, pgid=4242, mode="host")

    marker = tmp_path / "bakar-owner.marker"
    monkeypatch.setattr(
        build_stop,
        "lock_mutation_guard",
        lambda _cfg: build_stop.LockRefusal(
            reason=reason, host="peer-host" if reason == "peer-held" else None, marker=marker
        ),
    )
    killpg_calls = _record_killpg(monkeypatch)
    kill_pid_calls = _record_kill_pid(monkeypatch)
    _forbid_wait_and_escalate(monkeypatch)

    result = build_stop.stop_build(cfg.bsp_root, cfg)

    assert result is False
    assert killpg_calls == []
    assert kill_pid_calls == []
    # The refusal must fire before any run-dir/pidfile bookkeeping runs.
    assert (run_dir / "build.pid").exists()
    assert (run_dir / "build.meta.json").exists()
    err = capsys.readouterr().err
    assert str(marker) in err
    assert "by hand" in err


def test_stop_refusal_names_marker_and_manual_clear(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Both stop_build and stop_run print the marker path and the by-hand clear.

    The peer-held text keeps its "run `bakar stop` there" guidance, a refusal
    without a marker prints no manual-clear sentence, and no signal is sent.
    """
    cfg = make_build_config(workspace=tmp_path)
    run_dir = _make_run_dir_for_cfg(cfg)
    build_stop.write_launch_record(run_dir, pgid=4242, mode="host")
    marker = tmp_path / "bakar-owner.marker"
    killpg_calls = _record_killpg(monkeypatch)
    kill_pid_calls = _record_kill_pid(monkeypatch)
    _forbid_wait_and_escalate(monkeypatch)

    def run_both() -> list[str]:
        outputs = []
        for call in (
            lambda: build_stop.stop_build(cfg.bsp_root, cfg),
            lambda: build_stop.stop_run(run_dir, cfg),
        ):
            assert call() is False
            outputs.append(capsys.readouterr().err)
        return outputs

    monkeypatch.setattr(
        build_stop,
        "lock_mutation_guard",
        lambda _cfg: build_stop.LockRefusal(reason="peer-held", host="peer-host", marker=marker),
    )
    for err in run_both():
        assert "build owned by peer-host; run `bakar stop` there" in err
        assert str(marker) in err
        assert "by hand" in err

    monkeypatch.setattr(
        build_stop,
        "lock_mutation_guard",
        lambda _cfg: build_stop.LockRefusal(reason="unattributable", marker=marker),
    )
    for err in run_both():
        assert "cannot confirm this node owns the build lock (unattributable)" in err
        assert str(marker) in err
        assert "by hand" in err

    monkeypatch.setattr(
        build_stop,
        "lock_mutation_guard",
        lambda _cfg: build_stop.LockRefusal(reason="unattributable"),
    )
    for err in run_both():
        assert "by hand" not in err

    assert killpg_calls == []
    assert kill_pid_calls == []


def test_stop_build_local_marker_stop_path_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A local (or None-guard) marker leaves today's stop path unchanged.

    Mirrors ``test_stop_build_sigint_then_clean_exit`` but drives the same
    scenario through the ``cfg``-aware call, confirming the guard passing
    (``None``) does not alter the SIGINT-then-clean-exit behavior.
    """
    cfg = make_build_config(workspace=tmp_path)
    run_dir = _make_run_dir_for_cfg(cfg)
    build_stop.write_launch_record(run_dir, pgid=4242, mode="host")

    monkeypatch.setattr(build_stop, "lock_mutation_guard", lambda _cfg: None)
    monkeypatch.setattr(build_stop, "is_build_running", lambda _rd: (True, 4242, True))
    monkeypatch.setattr(proc_tree, "_pgid_alive", lambda _pgid: False)
    monkeypatch.setattr(build_stop.time, "sleep", lambda _s: None)
    calls = _record_killpg(monkeypatch)

    assert build_stop.stop_build(cfg.bsp_root, cfg) is True

    assert calls == [(4242, signal.SIGINT)]
    assert not (run_dir / "build.pid").exists()


def test_stop_build_no_cfg_skips_gate_and_uses_hardcoded_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cfg=None (the legacy call shape) skips the gate entirely - back-compat."""
    run_dir = _make_run_dir(tmp_path)
    build_stop.write_launch_record(run_dir, pgid=4242, mode="host")

    monkeypatch.setattr(build_stop, "is_build_running", lambda _rd: (True, 4242, True))
    monkeypatch.setattr(proc_tree, "_pgid_alive", lambda _pgid: False)
    monkeypatch.setattr(build_stop.time, "sleep", lambda _s: None)
    calls = _record_killpg(monkeypatch)

    assert build_stop.stop_build(tmp_path) is True

    assert calls == [(4242, signal.SIGINT)]


def test_stop_build_uses_build_dir_name_for_runs_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stop_build scans ``bsp_root/cfg.build_dir_name/runs``, not a hardcoded 'build'.

    Uses the qcom family, whose ``build_dir_name`` is ``build-<distro>`` - the
    hardcoded ``"build"`` path would scan a nonexistent directory and report
    "no running build" even though a run dir exists.
    """
    cfg = make_build_config(bsp_family="qcom", distro="qcom-wayland", workspace=tmp_path)
    assert cfg.build_dir_name == "build-qcom-wayland"
    run_dir = _make_run_dir_for_cfg(cfg)
    build_stop.write_launch_record(run_dir, pgid=4242, mode="host")
    # Confirm the hardcoded legacy path does NOT exist, so a pass here can only
    # be explained by using cfg.build_dir_name.
    assert not (cfg.bsp_root / "build" / "runs").exists()

    monkeypatch.setattr(build_stop, "lock_mutation_guard", lambda _cfg: None)
    monkeypatch.setattr(build_stop, "is_build_running", lambda _rd: (True, 4242, True))
    monkeypatch.setattr(proc_tree, "_pgid_alive", lambda _pgid: False)
    monkeypatch.setattr(build_stop.time, "sleep", lambda _s: None)
    calls = _record_killpg(monkeypatch)

    assert build_stop.stop_build(cfg.bsp_root, cfg) is True

    assert calls == [(4242, signal.SIGINT)]


def test_report_stale_cleanup_leaves_files_on_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_report_stale_cleanup removes nothing when the ownership gate refuses.

    Even though the node-local argv holder-scan would see no holder (empty
    process set) - the argv scan alone cannot see a peer's processes on a
    shared TOPDIR, so the gate refusal must win.
    """
    cfg, build_dir = _cfg_with_build_dir(tmp_path)
    run_dir = build_dir / "runs" / "20260618-120000"
    run_dir.mkdir(parents=True)
    (build_dir / "bitbake.lock").write_text("4242\n")
    (build_dir / "bitbake.sock").write_text("")

    monkeypatch.setattr(
        build_stop,
        "lock_mutation_guard",
        lambda _cfg: build_stop.LockRefusal(reason="peer-held", host="peer-host"),
    )

    removed = build_stop._report_stale_cleanup(run_dir, cfg)

    assert removed == []
    assert (build_dir / "bitbake.lock").exists()
    assert (build_dir / "bitbake.sock").exists()


def test_report_stale_cleanup_no_cfg_unchanged(tmp_path: Path) -> None:
    """cfg=None preserves the pre-guard node-local-only behavior."""
    run_dir = _make_run_dir(tmp_path)
    build_dir = run_dir.parent.parent
    (build_dir / "bitbake.lock").write_text("4242\n")

    removed = build_stop._report_stale_cleanup(run_dir)

    assert removed == [build_dir / "bitbake.lock"]
    assert not (build_dir / "bitbake.lock").exists()


def test_verify_clean_excludes_gate_refused_files_and_skips_liveness_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A gate-refused cleanup must not be reported as an incomplete stop.

    Correctly refusing to touch a peer's lock is the SUCCESSFUL outcome of
    ``bakar stop`` in that scenario - _verify_clean must exclude the
    gate-refused lock/socket files from its "still present" set and skip the
    server-liveness probe (a node-local PID read this node does not own).
    """
    cfg, build_dir = _cfg_with_build_dir(tmp_path)
    run_dir = build_dir / "runs" / "20260618-120000"
    run_dir.mkdir(parents=True)
    (build_dir / "bitbake.lock").write_text("4242\n")

    monkeypatch.setattr(
        build_stop,
        "lock_mutation_guard",
        lambda _cfg: build_stop.LockRefusal(reason="peer-held", host="peer-host"),
    )
    # If the liveness probe were NOT skipped, this would inject a failure reason.
    monkeypatch.setattr(proc_tree, "_bitbake_server_alive", lambda _rd: True)

    reasons = build_stop._verify_clean(run_dir, None, cfg=cfg)

    assert reasons == []


def test_verify_clean_no_cfg_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """cfg=None preserves prior behavior: a live bitbake-server IS reported."""
    run_dir = _make_run_dir(tmp_path)
    build_dir = run_dir.parent.parent
    (build_dir / "bitbake.lock").write_text("4242\n")

    monkeypatch.setattr(proc_tree, "_bitbake_server_alive", lambda _rd: True)

    reasons = build_stop._verify_clean(run_dir, None)

    assert any("bitbake-server" in r for r in reasons)


# --- _host_wide_candidates: shared-topdir host/container mode filter -------


def test_host_wide_candidates_excludes_container_mode_run_sharing_topdir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    nfs_probe: Callable[[bool | None], None],
) -> None:
    """A container-mode run sharing a topdir's ``runs/`` with a host-mode run
    must survive plain enumeration (proving the shared-topdir case is really
    exercised) but be excluded from ``stop_cmd._host_wide_candidates()``'s
    host-only filter.

    Discovery is driven end-to-end rather than stubbed at the
    ``_discover_host_cookers`` boundary: a fake PID/cmdline pair is fed
    through that function's own injectable ``pids_reader``/``cmdline_reader``
    parameters, so the topdir is found the same way a real ``/proc`` scan
    would find it, without a real bitbake process anywhere. The container
    leg's liveness is decided by monkeypatching
    ``stop_container._container_id_status`` directly, so the assertion holds
    whether or not a container runtime is installed on this host.
    """
    nfs_probe(False)

    # One shared topdir: <workspace>/nxp/build, holding both a host-mode and
    # a container-mode run under the same runs/ directory.
    workspace = tmp_path
    bsp_root = workspace / "nxp"
    topdir = bsp_root / "build"
    runs_dir = topdir / "runs"
    host_run_dir = runs_dir / "20260618-120000-host"
    container_run_dir = runs_dir / "20260618-130000-container"
    host_run_dir.mkdir(parents=True)
    container_run_dir.mkdir(parents=True)

    build_stop.write_launch_record(host_run_dir, pgid=4242, mode="host")
    build_stop.write_launch_record(
        container_run_dir,
        pgid=5252,
        mode="container",
        runtime="docker",
        container_label="bakar.run_id=container-run",
    )

    # Drive real discovery via a fake PID whose cmdline names this build's
    # bitbake.lock path - no real bitbake process, no real /proc scan.
    fake_pid = 987654
    lock_path = topdir / "bitbake.lock"
    real_discover_host_cookers = proc_tree._discover_host_cookers

    def _fake_discover_host_cookers() -> dict[Path, frozenset[int]]:
        return real_discover_host_cookers(
            pids_reader=lambda: [fake_pid],
            cmdline_reader=lambda pid: f"bitbake-server decafbad 5 6 log {lock_path} 0" if pid == fake_pid else "",
        )

    monkeypatch.setattr(proc_tree, "_discover_host_cookers", _fake_discover_host_cookers)

    # Host-mode leg is live; container-mode leg is reported alive by the
    # monkeypatched runtime query, independent of any real container runtime.
    monkeypatch.setattr(build_stop, "is_build_running", lambda _rd: (True, 4242, True))
    monkeypatch.setattr(
        stop_container,
        "_container_id_status",
        lambda _runtime, _label: (build_stop._ALIVE, "fake-cid"),
    )

    # First, prove enumeration itself is not mode-scoped: both runs show up
    # in the unfiltered scan over this shared topdir's runs/ directory.
    scan = run_discovery.enumerate_workspace_runs(runs_dir, user_config=None)
    unfiltered_dirs = {c.run_dir for c in scan.candidates}
    assert host_run_dir in unfiltered_dirs
    assert container_run_dir in unfiltered_dirs

    # Then, the stop command's host-wide filter must keep the host-mode run
    # and drop the container-mode run, even though both are live.
    candidates, _skipped = stop_cmd._host_wide_candidates()
    filtered_dirs = {c.run_dir for c in candidates}

    assert host_run_dir in filtered_dirs
    assert container_run_dir not in filtered_dirs


@pytest.mark.unit
def test_default_tasks_reader_is_incremental_and_matches_running_tasks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default wait reader never normalizes the whole log, yet agrees with running_tasks."""
    from bakar import eventlog
    from tests.test_eventlog import _task_event

    path = tmp_path / "bitbake_eventlog.json"
    first = _task_event("bb.build.TaskStarted", "a-1.0-r0", "do_compile", started=100.0)
    path.write_text(first + "\n", encoding="utf-8")

    def _boom(*_a: object, **_k: object) -> None:
        raise AssertionError("full normalize called during a wait poll")

    monkeypatch.setattr(eventlog, "normalize", _boom)
    read = build_stop._incremental_reader()

    got = read(tmp_path)
    assert [(t.recipe, t.task) for t in got] == [("a-1.0-r0", "do_compile")]

    with path.open("a", encoding="utf-8") as fh:
        fh.write(_task_event("bb.build.TaskSucceeded", "a-1.0-r0", "do_compile") + "\n")
        fh.write(_task_event("bb.build.TaskStarted", "b-1.0-r0", "do_fetch", started=101.0) + "\n")
    got = read(tmp_path)
    monkeypatch.undo()
    assert got == eventlog.running_tasks(tmp_path)
    assert [t.recipe for t in got] == ["b-1.0-r0"]


# --- graceful host stop signals only verified, positive pids ---------------


def _graceful_host_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lock_text: str, *, verified: bool | None
) -> list[tuple[int, int]]:
    """Run a graceful host ``stop_run`` and return every ``os.kill`` (pid, sig) it issued.

    ``verified`` stubs ``_bitbake_server_pid_verified``; ``None`` leaves the real
    check in place.
    """
    run_dir = _make_run_dir(tmp_path)
    build_stop.write_launch_record(run_dir, pgid=4242, mode="host")
    (run_dir.parent.parent / "bitbake.lock").write_text(lock_text)
    monkeypatch.setattr(build_stop, "is_build_running", lambda _rd: (True, 4242, True))
    monkeypatch.setattr(build_stop, "_graceful_wait", lambda **_kw: "drained")
    monkeypatch.setattr(build_stop, "_report_stale_cleanup", lambda *_a, **_k: [])
    monkeypatch.setattr(build_stop, "_verify_clean", lambda *_a, **_k: [])
    _record_killpg(monkeypatch)
    if verified is not None:
        monkeypatch.setattr(proc_tree, "_bitbake_server_pid_verified", lambda _pid, _topdir: verified)
    kills: list[tuple[int, int]] = []
    monkeypatch.setattr(build_stop.os, "kill", lambda pid, sig: kills.append((pid, sig)))
    assert build_stop.stop_run(run_dir) is True
    return kills


@pytest.mark.parametrize("pid", [-1, 0])
def test_graceful_stop_with_non_positive_lock_pid_sends_no_signal_to_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pid: int
) -> None:
    kills = _graceful_host_stop(tmp_path, monkeypatch, f"{pid}\n", verified=True)
    assert kills == []


def test_graceful_stop_does_not_signal_an_unverified_lock_pid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    kills = _graceful_host_stop(tmp_path, monkeypatch, "999999\n", verified=False)
    assert kills == []


def test_graceful_stop_signals_a_verified_lock_pid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    kills = _graceful_host_stop(tmp_path, monkeypatch, "999999\n", verified=True)
    assert kills == [(999999, signal.SIGINT)]


def test_graceful_stop_verifies_the_lock_pid_against_this_builds_topdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[int, Path]] = []

    def record(pid: int, topdir: Path) -> bool:
        seen.append((pid, topdir))
        return False

    run_dir = _make_run_dir(tmp_path)
    build_stop.write_launch_record(run_dir, pgid=4242, mode="host")
    (run_dir.parent.parent / "bitbake.lock").write_text("777\n")
    monkeypatch.setattr(build_stop, "is_build_running", lambda _rd: (True, 4242, True))
    monkeypatch.setattr(build_stop, "_graceful_wait", lambda **_kw: "drained")
    monkeypatch.setattr(build_stop, "_report_stale_cleanup", lambda *_a, **_k: [])
    monkeypatch.setattr(build_stop, "_verify_clean", lambda *_a, **_k: [])
    _record_killpg(monkeypatch)
    monkeypatch.setattr(proc_tree, "_bitbake_server_pid_verified", record)
    monkeypatch.setattr(build_stop.os, "kill", lambda p, s: pytest.fail("signalled an unverified pid"))
    build_stop.stop_run(run_dir)
    assert seen == [(777, run_dir.parent.parent)]
