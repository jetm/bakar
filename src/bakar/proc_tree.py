"""Process-tree walking and signalling for the stop path.

Leaf module: imports nothing from the bakar package.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable


def read_lock_pid(lock_path: Path) -> int | None:
    """Parse the PID bitbake wrote as the first token of ``bitbake.lock``.

    bitbake creates the lock and writes its PID as a later, separate step, so
    an empty or unparseable lock is a server mid-startup, not a stale
    leftover. Returns ``None`` for a missing or unreadable file, empty
    content, a non-integer first token, or a non-positive pid; callers must
    never treat ``None`` as license to remove the lock.
    """
    try:
        raw = lock_path.read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return None
    tokens = raw.split()
    if not tokens:
        return None
    try:
        pid = int(tokens[0])
    except ValueError:
        return None
    # -1 and 0 parse as ints but name no process: kill(-1, sig) signals every
    # process the user owns and kill(0, sig) this process's own group.
    if pid <= 0 or pid > _pid_max():
        return None
    return pid


def _pid_max() -> int:
    """The kernel's pid ceiling; 4194304 (the 64-bit default) when unreadable."""
    try:
        return int(Path("/proc/sys/kernel/pid_max").read_text().strip())
    except OSError, ValueError:
        return 4194304


def _read_bitbake_server_pid(run_dir: Path) -> int | None:
    """Read bitbake-server's own PID from ``bitbake.lock``'s first line.

    bb.server.process writes ``os.getpid()`` as the lock file's first
    line/token (see bitbake/lib/bb/server/process.py), and
    bb.daemonize.createDaemon double-forks + calls os.setsid() to start it -
    the server leads a brand-new session, so it is NEVER a member of the
    kas-container/kas process group ``build.pid`` records. killpg(pgid, ...)
    structurally cannot reach it: the server can (and by design does) outlive
    the client that launched it, e.g. to serve a warm cooker to a later
    bitbake invocation, or here, because something killed the client (a
    SIGQUIT from a job-control keypress) without the server ever seeing an
    interrupt. This is the only liveness signal that actually reaches it.

    Returns None when the lock is missing, empty, or unparseable - already
    exited, a mid-write race, or a container build with no host-side lock.
    """
    return read_lock_pid(run_dir.parent.parent / "bitbake.lock")


def _pid_alive(pid: int) -> bool:
    """Return True while process ``pid`` still exists (single-PID, not group)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError, OverflowError:
        return False
    except OSError:
        # EPERM: still alive, just owned by someone else.
        return True
    return True


def _bitbake_server_alive(run_dir: Path) -> bool:
    """True while bitbake-server's own detached PID (from bitbake.lock) is alive
    AND still identifiably this build's, not a PID the kernel has since
    recycled onto an unrelated process.

    Every caller of this function needs to agree with :func:`_escalate_host`'s
    kill decision, or the two diverge on a recycled PID: escalation correctly
    declines to signal it, while this function - unverified - would keep
    reporting the (unrelated) process as this build still running, forever.
    That reads to an operator as ``bakar stop`` never completing on a
    workspace whose real cooker has, in fact, already exited.
    """
    pid = _read_bitbake_server_pid(run_dir)
    return pid is not None and _pid_alive(pid) and _bitbake_server_pid_verified(pid, run_dir.parent.parent)


def _pgid_alive(pgid: int) -> bool:
    """Return True while any member of process group ``pgid`` still exists.

    Uses ``os.killpg(pgid, 0)`` (group semantics) so a build whose leader has
    exited but whose children are still shutting down counts as alive.
    """
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError, OverflowError:
        return False
    except OSError:
        # EPERM: still alive, just owned by someone else.
        return True
    return True


# bitbake-server's argv carries this build's absolute logfile, lock and socket
# paths (bitbake/lib/bb/server/process.py execServer execl). Matching the FULL
# build-specific path - never a bare basename - is what scopes discovery to THIS
# build: a second build on the same host has a different TOPDIR, so its cooker's
# argv references a different bitbake.lock path and can never match here.
_COOKER_ARGV_FILES = ("bitbake.lock", "bitbake.sock", "bitbake-cookerdaemon.log")


_PROC_ROOT = Path("/proc")


@dataclass(frozen=True)
class _ScopedProcs:
    """The process set attributable to one build dir.

    ``cooker`` is the argv-matched set - the wedged cooker and anything else
    spawned with this build's lock/sock/cookerdaemon.log paths (the lock
    holders). ``all_pids`` is ``cooker`` plus the recorded wrapper-PGID members
    plus the transitive /proc-ppid descendants of both, minus this ``bakar
    stop`` process and its own group. Signalling ``all_pids`` reaches the
    bitbake-worker and reparented-orphan processes that the PGID and lock-PID
    paths miss.
    """

    cooker: frozenset[int]
    all_pids: frozenset[int]


def _all_pids(proc_root: Path = _PROC_ROOT) -> list[int]:
    """Return every numeric PID under ``proc_root`` (/proc); [] on error."""
    try:
        return sorted(int(entry.name) for entry in proc_root.iterdir() if entry.name.isdigit())
    except OSError:
        return []


def _proc_cmdline(pid: int, proc_root: Path = _PROC_ROOT) -> str:
    """Return ``pid``'s space-joined argv, or '' when unreadable (gone/EPERM)."""
    try:
        raw = (proc_root / str(pid) / "cmdline").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\x00", b" ").decode("utf-8", "replace")


def _proc_ppid(pid: int, proc_root: Path = _PROC_ROOT) -> int | None:
    """Return ``pid``'s parent PID from /proc/<pid>/status, or None on error."""
    try:
        raw = (proc_root / str(pid) / "status").read_text()
    except OSError:
        return None
    for line in raw.splitlines():
        if line.startswith("PPid:"):
            fields = line.split()
            if len(fields) < 2:
                return None
            try:
                return int(fields[1])
            except ValueError:
                return None
    return None


@dataclass(frozen=True, kw_only=True, slots=True)
class _ProcView:
    """How the reaper observes /proc, and which process is the observer itself."""

    self_pid: int | None = None
    self_pgid: int | None = None
    pids_reader: Callable[[], list[int]] = _all_pids
    cmdline_reader: Callable[[int], str] = _proc_cmdline
    ppid_reader: Callable[[int], int | None] = _proc_ppid
    pgid_reader: Callable[[int], int] = os.getpgid


_LIVE_PROC_VIEW = _ProcView()


def _collect_build_pids(
    topdir: Path,
    pgid: int | None,
    view: _ProcView = _LIVE_PROC_VIEW,
) -> _ScopedProcs:
    """Collect the process set attributable to the build rooted at ``topdir``.

    Seeds are the argv-matched cooker (any process whose cmdline references this
    build's lock/sock/cookerdaemon.log path) plus the recorded wrapper-PGID
    members. The result adds their transitive /proc-ppid descendants (workers,
    task subprocesses, orphans reparented to init that are still descendants),
    then drops this ``bakar stop`` process and its own group so the reaper can
    never signal itself. The readers are injectable so the /proc walk is
    unit-testable against a fake procfs without real processes.
    """
    resolved_self_pid = os.getpid() if view.self_pid is None else view.self_pid
    resolved_self_pgid = os.getpgrp() if view.self_pgid is None else view.self_pgid
    markers = [str(topdir / name) for name in _COOKER_ARGV_FILES]

    pids = view.pids_reader()
    cmdlines = {pid: view.cmdline_reader(pid) for pid in pids}
    cooker = {pid for pid in pids if any(marker in cmdlines[pid] for marker in markers)}

    seeds = set(cooker)
    if pgid is not None and pgid > 0:
        for pid in pids:
            try:
                if view.pgid_reader(pid) == pgid:
                    seeds.add(pid)
            except OSError:
                continue

    if not seeds:
        # cooker is a subset of seeds, so an empty seed set means no cooker and
        # no group member - nothing attributable to this build. Skip the ppid
        # walk entirely (the common clean-tree and hermetic-test case).
        return _ScopedProcs(cooker=frozenset(), all_pids=frozenset())

    children: dict[int, list[int]] = {}
    for pid in pids:
        parent = view.ppid_reader(pid)
        if parent is not None:
            children.setdefault(parent, []).append(pid)

    reached = set(seeds)
    stack = list(seeds)
    while stack:
        current = stack.pop()
        for child in children.get(current, ()):
            if child not in reached:
                reached.add(child)
                stack.append(child)

    def _is_self(pid: int) -> bool:
        if pid == resolved_self_pid:
            return True
        try:
            return view.pgid_reader(pid) == resolved_self_pgid
        except OSError:
            return False

    return _ScopedProcs(
        cooker=frozenset(pid for pid in cooker if not _is_self(pid)),
        all_pids=frozenset(pid for pid in reached if not _is_self(pid)),
    )


def _discover_host_cookers(
    *,
    pids_reader: Callable[[], list[int]] = _all_pids,
    cmdline_reader: Callable[[int], str] = _proc_cmdline,
) -> dict[Path, frozenset[int]]:
    """Scan every PID on the host for a bitbake cooker's argv markers.

    Unlike :func:`_collect_build_pids`, this takes no ``topdir`` up front - it
    walks every host PID looking for the three bare marker filenames
    (``bitbake.lock``, ``bitbake.sock``, ``bitbake-cookerdaemon.log``)
    anywhere in that process's cmdline, rather than a full path under one
    known build dir. For each match the topdir is recovered by stripping the
    trailing marker filename off the matched path (``.../nxp/build/
    bitbake.lock`` -> ``.../nxp/build``).

    A ``bitbake-worker`` subprocess is spawned with piped file descriptors
    and carries no lock/socket/log path in its own argv, so the same argv
    match that discovers a topdir also identifies its cooker - there is no
    separate worker-exclusion step. This is treated as a verified assumption
    here, checked by a dedicated multi-worker test rather than asserted only
    in this docstring.

    A PID whose cmdline cannot be read (permission denied on a multi-user
    host) resolves to ``""`` from ``cmdline_reader`` and is skipped, not
    raised on.

    Returns a dict mapping each discovered topdir to the frozenset of cooker
    PID(s) whose argv matched there. Deduplication across topdirs (e.g. the
    same build discovered via both its lock and its log path) and
    correlation with a recorded run_id are out of scope - later tasks own
    those.
    """
    discovered: dict[Path, set[int]] = {}
    for pid in pids_reader():
        # Guards the call site itself rather than trusting the documented
        # "never raises" contract alone: the default `_proc_cmdline` already
        # honors it, but `cmdline_reader` is an injectable parameter with no
        # type-level enforcement, and one PID's unreadable cmdline must not
        # abort the whole host-wide scan.
        try:
            cmdline = cmdline_reader(pid)
        except OSError:
            continue
        if not cmdline:
            continue
        for token in cmdline.split():
            for marker in _COOKER_ARGV_FILES:
                suffix = f"/{marker}"
                if token.endswith(suffix):
                    topdir = Path(token[: -len(suffix)])
                    discovered.setdefault(topdir, set()).add(pid)
                    break

    return {topdir: frozenset(pids) for topdir, pids in discovered.items()}


def _kill_pid(pid: int, sig: int) -> bool:
    """``os.kill`` ``pid`` with ``sig``, guarding the dangerous pids.

    A ``pid <= 0`` is refused outright, like :func:`_killpg` refuses a
    non-positive group id: ``kill(-1, ...)`` signals every process the user can
    reach and ``kill(0, ...)`` this process's own group. Returns True when the
    signal was delivered, False when the process is gone, denied, or refused.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, sig)
    except OSError:
        return False
    return True


def _killpg(pgid: int, sig: int) -> bool:
    """``os.killpg`` ``pgid`` with ``sig``, guarding the dangerous group ids.

    A ``pgid <= 0`` is refused outright: ``killpg(0, ...)`` would signal this
    ``bakar stop`` process's own group and ``killpg(-1, ...)`` every process the
    user can reach. Returns True when the signal was delivered, False when the
    group is gone, denied, or refused by the guard.
    """
    if pgid <= 0:
        return False
    try:
        os.killpg(pgid, sig)
    except OSError:
        return False
    return True


def _host_build_alive(pgid: int | None, run_dir: Path) -> bool:
    """True while ANY part of the host build still runs.

    Checks three layers so a wedged cooker whose wrapper already died still
    reads as alive: the wrapper process group, bitbake-server's detached PID
    from ``bitbake.lock``, and the argv-scoped cooker set (which catches a
    cooker whose lock first line is unreadable).
    """
    if pgid is not None and pgid > 0 and _pgid_alive(pgid):
        return True
    if _bitbake_server_alive(run_dir):
        return True
    return bool(_collect_build_pids(run_dir.parent.parent, None).cooker)


def _bitbake_server_pid_verified(bb_pid: int, topdir: Path) -> bool:
    """True unless ``bb_pid`` is POSITIVELY known not to be this build's own
    bitbake-server.

    ``_read_bitbake_server_pid`` returns a raw PID parsed from ``bitbake.lock``,
    with no check tying that number back to a live bitbake-server process. The
    gap between reading the lock and signalling widens with every escalation
    ladder this ladder now feeds (a 900s capture timeout routes here too), and
    across a wide enough gap the kernel can recycle that PID onto an unrelated
    process - SIGKILL-ing it would kill something this build never launched.
    bitbake's own execServer (bb.server.process) execs with the lock file's and
    socket's absolute paths as literal argv tokens, so a genuine bitbake-server
    always carries one; anything else reading at that PID does not.

    Returns False only on positive evidence: the PID has already exited, or its
    live cmdline names a different lock/sock path. An UNREADABLE cmdline
    (``/proc`` mounted ``hidepid=1``/``2``, the cooker running as another uid) is
    not that evidence - conflating "could not verify" with "verified not ours"
    would silently disable this layer on any host with a restricted ``/proc``,
    and :func:`_bitbake_server_alive` would then report a live cooker as
    running forever with no path left to ever kill it. An unreadable cmdline
    therefore falls back to True: the escalation ladder accepts the same
    residual recycled-PID risk this function otherwise closes, rather than
    trade a rare bad kill for a permanently stuck lock.
    """
    if not _pid_alive(bb_pid):
        return False
    cmdline = _proc_cmdline(bb_pid)
    if not cmdline:
        return True
    markers = [str(topdir / name) for name in ("bitbake.lock", "bitbake.sock")]
    return any(marker in cmdline for marker in markers)
