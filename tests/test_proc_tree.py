"""Unit tests for bakar.proc_tree process primitives.

Covers the bitbake server-pid readers and verifier, liveness probes, the
injectable process view behind the scoped /proc reaper, host-wide cooker
discovery, and the signal-refusal guards for non-positive pids and groups.
Every test is hermetic: the /proc readers are injected and no signal reaches a
real process.
"""

from __future__ import annotations

import os
import signal
import subprocess
from typing import TYPE_CHECKING

import pytest

from bakar import proc_tree

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


def _make_run_dir(bsp_root: Path, run_id: str = "20260618-120000") -> Path:
    """Create ``bsp_root/build/runs/<run_id>`` and return it."""
    run_dir = bsp_root / "build" / "runs" / run_id
    run_dir.mkdir(parents=True)
    return run_dir


# --- bitbake server pid readers and liveness --------------------------------


def test_read_bitbake_server_pid_from_lock_file(tmp_path: Path) -> None:
    """The PID is read from bitbake.lock's first line, topdir = run_dir/../..."""
    run_dir = _make_run_dir(tmp_path)
    (run_dir.parent.parent / "bitbake.lock").write_text("424242\n")

    assert proc_tree._read_bitbake_server_pid(run_dir) == 424242


def test_read_bitbake_server_pid_missing_lock_returns_none(tmp_path: Path) -> None:
    run_dir = _make_run_dir(tmp_path)

    assert proc_tree._read_bitbake_server_pid(run_dir) is None


def test_read_bitbake_server_pid_unparseable_lock_returns_none(tmp_path: Path) -> None:
    """A corrupt or truncated lock file must not raise - just report unknown."""
    run_dir = _make_run_dir(tmp_path)
    (run_dir.parent.parent / "bitbake.lock").write_text("not-a-pid\n")

    assert proc_tree._read_bitbake_server_pid(run_dir) is None


def test_pid_alive_true_for_current_process() -> None:
    assert proc_tree._pid_alive(os.getpid()) is True


def test_pid_alive_false_for_reaped_process() -> None:
    """A PID that has already exited and been reaped reports not-alive."""
    proc = subprocess.Popen(["true"])
    proc.wait()

    assert proc_tree._pid_alive(proc.pid) is False


def test_bitbake_server_alive_true_when_lock_pid_is_alive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_dir = _make_run_dir(tmp_path)
    (run_dir.parent.parent / "bitbake.lock").write_text(f"{os.getpid()}\n")
    # This test process's own cmdline is real pytest argv, not this fake
    # topdir's bitbake.lock path - stub verification, which is unit-tested on
    # its own below, so this test stays scoped to liveness alone.
    monkeypatch.setattr(proc_tree, "_bitbake_server_pid_verified", lambda _pid, _topdir: True)

    assert proc_tree._bitbake_server_alive(run_dir) is True


def test_bitbake_server_alive_false_when_lock_missing(tmp_path: Path) -> None:
    run_dir = _make_run_dir(tmp_path)

    assert proc_tree._bitbake_server_alive(run_dir) is False


# --- bitbake server pid verification ----------------------------------------


def test_bitbake_server_pid_verified_true_when_cmdline_references_this_builds_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    topdir = tmp_path
    lock_path = topdir / "bitbake.lock"
    sock_path = topdir / "bitbake.sock"
    monkeypatch.setattr(proc_tree, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(
        proc_tree,
        "_proc_cmdline",
        lambda _pid: f"/usr/bin/python3 bitbake-server decafbad 5 6 log {lock_path} {sock_path} 0",
    )

    assert proc_tree._bitbake_server_pid_verified(999999, topdir) is True


def test_bitbake_server_pid_verified_false_for_an_unrelated_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live, but non-matching, cmdline is positive evidence the PID is not
    this build's own bitbake-server."""
    monkeypatch.setattr(proc_tree, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(proc_tree, "_proc_cmdline", lambda _pid: "/usr/bin/some-unrelated-daemon")

    assert proc_tree._bitbake_server_pid_verified(999999, tmp_path) is False


def test_bitbake_server_pid_verified_false_for_a_different_builds_lock_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Matching must be scoped to THIS build's full topdir path, not merely
    the bare basename - a cmdline naming another build's bitbake.lock is a
    live bitbake-server, just not this one's, and must not verify."""
    other_topdir = tmp_path / "some-other-build"
    monkeypatch.setattr(proc_tree, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(
        proc_tree,
        "_proc_cmdline",
        lambda _pid: (
            f"/usr/bin/python3 bitbake-server decafbad 5 6 log "
            f"{other_topdir / 'bitbake.lock'} {other_topdir / 'bitbake.sock'} 0"
        ),
    )

    assert proc_tree._bitbake_server_pid_verified(999999, tmp_path / "this-build") is False


def test_bitbake_server_pid_verified_false_when_pid_no_longer_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(proc_tree, "_pid_alive", lambda _pid: False)

    assert proc_tree._bitbake_server_pid_verified(999999, tmp_path) is False


def test_bitbake_server_pid_verified_true_when_cmdline_is_unreadable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreadable cmdline (hidepid, a different uid) is NOT evidence the PID
    is stale - it is unverifiable, and this function must not conflate the
    two or a hardened /proc silently disables the whole escalation layer."""
    monkeypatch.setattr(proc_tree, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(proc_tree, "_proc_cmdline", lambda _pid: "")

    assert proc_tree._bitbake_server_pid_verified(999999, tmp_path) is True


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

    scoped = proc_tree._collect_build_pids(
        topdir,
        None,
        proc_tree._ProcView(
            self_pid=1,
            self_pgid=1,
            pids_reader=pids,
            cmdline_reader=cmdline,
            ppid_reader=ppid,
            pgid_reader=pgid,
        ),
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

    scoped = proc_tree._collect_build_pids(
        topdir,
        100,
        proc_tree._ProcView(
            self_pid=1,
            self_pgid=1,
            pids_reader=pids,
            cmdline_reader=cmdline,
            ppid_reader=ppid,
            pgid_reader=pgid,
        ),
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

    scoped = proc_tree._collect_build_pids(
        topdir,
        None,
        proc_tree._ProcView(
            self_pid=42,
            self_pgid=42,
            pids_reader=pids,
            cmdline_reader=cmdline,
            ppid_reader=ppid,
            pgid_reader=pgid,
        ),
    )

    assert 42 not in scoped.all_pids  # never signal ourselves
    assert 43 not in scoped.all_pids  # nor our own group
    assert scoped.cooker == frozenset({555})


def test_collect_build_pids_empty_when_no_match(tmp_path: Path) -> None:
    """No argv match and no PGID members -> empty set (clean-tree short circuit)."""
    topdir = tmp_path / "build"
    procs = {10: (1, "unrelated"), 11: (1, "also unrelated")}
    pids, cmdline, ppid, pgid = _fake_proc(procs)

    scoped = proc_tree._collect_build_pids(
        topdir,
        None,
        proc_tree._ProcView(
            self_pid=1,
            self_pgid=1,
            pids_reader=pids,
            cmdline_reader=cmdline,
            ppid_reader=ppid,
            pgid_reader=pgid,
        ),
    )

    assert scoped.cooker == frozenset()
    assert scoped.all_pids == frozenset()


# --- host-wide discovery: _discover_host_cookers ---------------------------
#
# Unlike _collect_build_pids above, this scan has no known topdir up front -
# it walks every host PID and recovers the topdir from whichever marker
# filename shows up in that process's own argv.


def test_discover_host_cookers_finds_topdir_from_bare_marker_filename(tmp_path: Path) -> None:
    """A cooker's argv is matched on the bare marker filename, topdir is recovered from it."""
    topdir = tmp_path / "nxp" / "build"
    sock = topdir / "bitbake.sock"
    procs = {
        4242: (1, f"python bitbake-server decafbad 7 8 {sock} idle"),
        9000: (1, "unrelated-process --flag"),
    }
    pids, cmdline, _ppid, _pgid = _fake_proc(procs)

    discovered = proc_tree._discover_host_cookers(pids_reader=pids, cmdline_reader=cmdline)

    assert discovered == {topdir: frozenset({4242})}


def test_discover_host_cookers_matches_all_three_marker_filenames(tmp_path: Path) -> None:
    """lock, sock, and cookerdaemon.log paths each resolve to their own topdir."""
    lock_dir = tmp_path / "a" / "build"
    sock_dir = tmp_path / "b" / "build"
    log_dir = tmp_path / "c" / "build"
    procs = {
        1: (0, f"bitbake-server {lock_dir / 'bitbake.lock'}"),
        2: (0, f"bitbake-server {sock_dir / 'bitbake.sock'}"),
        3: (0, f"bitbake-server {log_dir / 'bitbake-cookerdaemon.log'}"),
    }
    pids, cmdline, _ppid, _pgid = _fake_proc(procs)

    discovered = proc_tree._discover_host_cookers(pids_reader=pids, cmdline_reader=cmdline)

    assert discovered == {
        lock_dir: frozenset({1}),
        sock_dir: frozenset({2}),
        log_dir: frozenset({3}),
    }


def test_discover_host_cookers_worker_subprocess_has_no_argv_marker(tmp_path: Path) -> None:
    """A bitbake-worker's own argv carries no marker path, so it is never mistaken for a cooker.

    Verifies the cooker-identification rule stated in this function's docstring: the
    worker is spawned with piped fds and does not itself reference the lock/sock/log
    path, so the plain argv-marker match already excludes it with no separate step.
    """
    topdir = tmp_path / "build"
    lock = topdir / "bitbake.lock"
    procs = {
        4242: (1, f"python bitbake-server decafbad 7 8 {lock} idle"),
        4243: (4242, "bitbake-worker decafbad"),  # spawned with piped fds, no path in argv
    }
    pids, cmdline, _ppid, _pgid = _fake_proc(procs)

    discovered = proc_tree._discover_host_cookers(pids_reader=pids, cmdline_reader=cmdline)

    assert discovered == {topdir: frozenset({4242})}  # only the cooker, never the worker


def test_discover_host_cookers_multiple_builds_on_host(tmp_path: Path) -> None:
    """Two unrelated builds on the same host resolve to two distinct topdirs."""
    topdir_a = tmp_path / "nxp" / "build"
    topdir_b = tmp_path / "ti" / "build"
    procs = {
        100: (1, f"bitbake-server {topdir_a / 'bitbake.lock'}"),
        200: (1, f"bitbake-server {topdir_b / 'bitbake.lock'}"),
    }
    pids, cmdline, _ppid, _pgid = _fake_proc(procs)

    discovered = proc_tree._discover_host_cookers(pids_reader=pids, cmdline_reader=cmdline)

    assert discovered == {topdir_a: frozenset({100}), topdir_b: frozenset({200})}


def test_discover_host_cookers_skips_unreadable_cmdline(tmp_path: Path) -> None:
    """A PID whose cmdline read returns '' (the default `_proc_cmdline`'s
    documented OSError -> '' contract) is skipped, not raised on."""
    topdir = tmp_path / "build"
    lock = topdir / "bitbake.lock"
    procs = {
        4242: (1, f"bitbake-server {lock}"),
        5000: (1, "should-never-be-read"),  # stands in for a denied /proc/<pid>/cmdline
    }
    pids, cmdline, _ppid, _pgid = _fake_proc(procs)

    def _cmdline_permission_denied(pid: int) -> str:
        if pid == 5000:
            return ""  # matches _proc_cmdline's OSError -> "" contract
        return cmdline(pid)

    discovered = proc_tree._discover_host_cookers(pids_reader=pids, cmdline_reader=_cmdline_permission_denied)

    assert discovered == {topdir: frozenset({4242})}


def test_discover_host_cookers_skips_pid_whose_cmdline_reader_raises(tmp_path: Path) -> None:
    """A `cmdline_reader` that actually RAISES PermissionError for one PID -
    not just one that returns '' - is guarded at the call site: that PID is
    skipped and the scan still finds every other cooker, rather than the
    whole host-wide walk aborting on one unreadable process."""
    topdir = tmp_path / "build"
    lock = topdir / "bitbake.lock"
    procs = {4242: (1, f"bitbake-server {lock}")}
    pids, cmdline, _ppid, _pgid = _fake_proc(procs)

    def _cmdline_raises(pid: int) -> str:
        if pid == 5000:
            raise PermissionError("denied")
        return cmdline(pid)

    discovered = proc_tree._discover_host_cookers(pids_reader=lambda: [*pids(), 5000], cmdline_reader=_cmdline_raises)

    assert discovered == {topdir: frozenset({4242})}


def test_discover_host_cookers_empty_when_no_match(tmp_path: Path) -> None:
    """No process on the host references a marker filename -> empty dict."""
    procs = {10: (1, "unrelated"), 11: (1, "also unrelated")}
    pids, cmdline, _ppid, _pgid = _fake_proc(procs)

    discovered = proc_tree._discover_host_cookers(pids_reader=pids, cmdline_reader=cmdline)

    assert discovered == {}


# --- _killpg guard ---------------------------------------------------------


def test_killpg_refuses_zero_and_negative(monkeypatch: pytest.MonkeyPatch) -> None:
    """_killpg refuses pgid <= 0 (would hit our own group / every process)."""
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(proc_tree.os, "killpg", lambda pgid, sig: calls.append((pgid, sig)))

    assert proc_tree._killpg(0, signal.SIGKILL) is False
    assert proc_tree._killpg(-1, signal.SIGKILL) is False
    assert proc_tree._killpg(4242, signal.SIGTERM) is True

    assert calls == [(4242, signal.SIGTERM)]  # only the valid group was signalled


# --- non-positive pid refusals ---------------------------------------------


@pytest.mark.parametrize("pid", [-1, 0])
def test_read_lock_pid_rejects_non_positive_pids(tmp_path: Path, pid: int) -> None:
    lock = tmp_path / "bitbake.lock"
    lock.write_text(f"{pid}\n")
    assert proc_tree.read_lock_pid(lock) is None


@pytest.mark.parametrize("pid", [-1, 0])
def test_kill_pid_refuses_non_positive_pids(monkeypatch: pytest.MonkeyPatch, pid: int) -> None:
    kills: list[tuple[int, int]] = []
    monkeypatch.setattr(proc_tree.os, "kill", lambda p, s: kills.append((p, s)))
    assert proc_tree._kill_pid(pid, signal.SIGINT) is False
    assert kills == []
