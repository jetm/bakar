"""Bitbake lock/ownership arbitration for the kas build step.

Split out of :mod:`bakar.steps.kas_build`. Consumers import these
names from this module directly (``kas_graph_capture`` takes
``_lock_holder_has_activity`` from here), so a test patches a name in the module
that looks it up: ``bakar.steps.kas_lock`` for calls made inside this module,
and the consuming module's own namespace for a name it imported. The NFS probe
is the exception: it is looked up through :mod:`bakar.mounts` at call time, so
one patch of ``bakar.mounts.is_path_on_nfs`` reaches every NFS decision.
"""

from __future__ import annotations

import contextlib
import errno
import os
import socket
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from rich.markup import escape

from bakar import build_stop, mounts, observability, proc_tree, stop_container
from bakar.fmt import neutralize

if TYPE_CHECKING:
    from collections.abc import Iterator

    from bakar.config import BuildConfig
    from bakar.observability import RunLogger


def _lock_pid_is_live_bitbake(pid: int) -> bool:
    """True when ``pid`` is a live process that looks like bitbake.

    Mirrors today's node-local liveness probe: a dead PID (``ProcessLookupError``)
    or a live PID whose ``/proc/<pid>/cmdline`` does not mention ``bitbake``
    (PID reuse) is NOT a live bitbake process. A live PID this node cannot
    read ``cmdline`` for (``PermissionError``, or the ``/proc`` entry raced
    away) is conservatively treated as a live bitbake process - the lock is
    left alone rather than risk deleting a real build's lock.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    cmdline_path = Path(f"/proc/{pid}/cmdline")
    if cmdline_path.exists():
        try:
            raw = cmdline_path.read_bytes()
        except OSError:
            return True
        cmdline = raw.replace(b"\x00", b" ").decode(errors="replace")
        return "bitbake" in cmdline.lower()
    return True


def _lock_holder_has_activity(build_dir: Path) -> bool:
    """True when the lock holder's process tree has activity beyond a bare idle server.

    bitbake's cookerdaemon can persist after a build finishes (a nonzero
    ``BB_SERVER_TIMEOUT``, or stress-parse's persistent-server mode) so later
    invocations reconnect instead of re-spawning - a documented happy path.
    A bare idle server must NOT trip ``held-locally``, or the very next
    ``bakar build`` on this node would refuse instead of reconnecting.

    Reuses :func:`proc_tree._collect_build_pids`' argv-scan machinery: its
    ``all_pids`` is the argv-matched cooker plus PGID members plus their
    transitive ``/proc``-ppid descendants (workers, clients). More than the
    bare cooker PID itself in that set means genuine worker/client activity.

    Defined here and imported by name into
    :mod:`bakar.steps.kas_graph_capture`. Each importer holds its own binding,
    so patch the module whose code does the lookup: ``kas_lock`` for this
    module's calls, ``kas_graph_capture`` for its ``_wait_for_cooker_idle``.
    """
    procs = proc_tree._collect_build_pids(build_dir, None)
    return len(procs.all_pids) > 1


# An empty or unreadable bitbake.lock beside this host's marker reads as a build
# still starting up (bitbake creates the lock and writes its pid as a separate
# later step) only while the newest run is younger than this.
_EMPTY_LOCK_RECENT_RUN_S = 300.0


def _newest_run_dir(cfg: BuildConfig) -> Path | None:
    try:
        run_dirs = sorted(d for d in cfg.runs_dir.iterdir() if d.is_dir())
    except OSError:
        return None
    return run_dirs[-1] if run_dirs else None


def _run_id_pid(run_dir: Path) -> int | None:
    """The bakar pid a ``<timestamp>-<pid>`` run directory name ends with."""
    head, sep, tail = run_dir.name.rpartition("-")
    if not sep or len(head) != observability.RUN_ID_TS_LEN or not tail.isdigit():
        return None
    pid = int(tail)
    return pid if pid > 0 else None


def _launch_record_state(run_dir: Path | None) -> tuple[bool, int | None, int | None]:
    """``(live, seed_pgid, report_pid)`` for the launch record in ``run_dir``.

    ``live``: a host record whose process group is alive and cmdline-verified,
    or a container record whose label the runtime does not confirm dead (an
    unanswerable query is not proof the container is gone). ``seed_pgid`` is
    the recorded group whenever it is alive, verified or not, so the argv scan
    counts its members (an attached client in its own session). ``report_pid``
    is the host pid to name in a refusal.
    """
    if run_dir is None:
        return False, None, None
    alive, pgid, cmdline_ok = build_stop.is_build_running(run_dir)
    seed_pgid = pgid if alive and pgid is not None and pgid > 0 else None
    record = build_stop.read_launch_record(run_dir)
    report_pid = seed_pgid or record.pgid or _run_id_pid(run_dir)
    live = alive and cmdline_ok
    if not live and record.mode == "container" and record.container_label is not None:
        runtime = record.runtime or stop_container.detect_runtime()
        status, _cid = stop_container._container_id_status(runtime, record.container_label)
        live = status != stop_container._DEAD
    # A live record with no pid anywhere (a sidecar without pgid, a run
    # directory not named by RunLogger) cannot be named; RunLogger and
    # write_launch_record never produce one.
    return live and report_pid is not None, seed_pgid, report_pid


def _proc_comm(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/comm").read_text().strip()
    except OSError:
        return ""


def _cooker_provably_idle(build_dir: Path, pid: int, seed_pgid: int | None) -> bool:
    """True only on positive evidence that ``pid`` is this build's idle cooker.

    Requires the pid to be argv-matched to this build directory (under its
    given or resolved path, since a symlinked workspace puts the resolved
    path in bitbake-server's argv) and verified by
    :func:`proc_tree._bitbake_server_pid_verified`, and the scan seeded with
    the launch record's group to find nothing else: no worker, client or other
    child. The one tolerated child is a fork of the cooker that never exec'd
    (identical argv, parent is the cooker) and is not a ``Parser-N`` process:
    that is the hashserv bitbake autostarts under ``BB_HASHSERVE=auto``, which
    lives as long as a warm idle cooker does.
    """
    topdirs = {build_dir}
    with contextlib.suppress(OSError, RuntimeError):
        topdirs.add(build_dir.resolve())
    cooker: set[int] = set()
    others: set[int] = set()
    for topdir in topdirs:
        scan = proc_tree._collect_build_pids(topdir, seed_pgid)
        if pid in scan.cooker and proc_tree._bitbake_server_pid_verified(pid, topdir):
            cooker.add(pid)
        others |= scan.all_pids
    if pid not in cooker:
        return False
    cooker_argv = proc_tree._proc_cmdline(pid)
    for other in others - {pid}:
        forked_service = (
            proc_tree._proc_ppid(other) == pid
            and proc_tree._proc_cmdline(other) == cooker_argv
            and not _proc_comm(other).startswith("Parser")
        )
        if not forked_service:
            return False
    return True


def local_build_holder(cfg: BuildConfig) -> int | None:
    """Pid of a live local build holding ``bitbake.lock``, else None.

    Non-mutating probe for destructive callers:
    :func:`build_stop.lock_mutation_guard` never reports ``held-locally``, so
    this layers an activity probe on top. It fails closed: a live bitbake pid
    in the lock is a holder unless the build is provably idle (see
    :func:`_cooker_provably_idle`) AND no live launch record (the newest run
    under ``cfg.runs_dir``) names this build directory. A bitbake-server with
    no child yet (task initialisation, sstate availability checks) or with only
    an attached client therefore still counts.

    A lock pid that is not a live bitbake on this host is a container-namespace
    pid when a live container launch record exists, so the record decides. An
    empty or unreadable lock is a server mid-startup when a live launch record
    exists, or when the marker names this host and the newest run started
    within :data:`_EMPTY_LOCK_RECENT_RUN_S`. An absent lock, a warm idle cooker
    with no launch record, and a dead or reused pid with no record return None.
    Never removes or writes anything.
    """
    build_dir = cfg.bsp_root / cfg.build_dir_name
    lock = build_dir / "bitbake.lock"
    if not lock.exists():
        return None
    run_dir = _newest_run_dir(cfg)
    record_live, seed_pgid, report_pid = _launch_record_state(run_dir)
    pid = proc_tree.read_lock_pid(lock)

    if pid is None:
        if record_live:
            return report_pid
        if run_dir is None or build_stop.read_marker_owner(cfg) != socket.gethostname():
            return None
        started = observability.run_id_epoch(run_dir.name)
        if started is None or time.time() - started > _EMPTY_LOCK_RECENT_RUN_S:
            return None
        return _run_id_pid(run_dir)

    if _lock_pid_is_live_bitbake(pid):
        if record_live or not _cooker_provably_idle(build_dir, pid, seed_pgid):
            return pid
        return None
    if record_live:
        return report_pid
    return None


def clear_stale_bitbake_locks(cfg: BuildConfig) -> build_stop.LockClearOutcome:
    """Ownership-aware removal of stale bitbake lock and socket files.

    BitBake writes its PID into ``<build>/bitbake.lock`` at startup and
    removes it on clean exit. A crash leaves the lock and both Unix sockets
    (``bitbake.sock``, ``hashserve.sock``) behind, causing the next
    invocation to refuse to start ("bitbake is already running") - but on a
    shared NFS TOPDIR that "stale" lock may belong to a live build on a peer
    fleet node, so ownership is checked BEFORE absence/staleness in every
    branch below (never the reverse):

    1. The ownership marker (:func:`build_stop.lock_marker_path`) names a
       PEER host -> refuse (``peer-held``); nothing is touched.
    2. The marker names THIS host and ``bitbake.lock`` is absent -> remove
       leftover sockets and this node's own marker.
    3. The marker names THIS host and ``bitbake.lock`` is present -> probe
       the recorded PID: a live bitbake process WITH worker/client activity
       refuses (``held-locally``); a live but IDLE bitbake process (bare
       cookerdaemon) is left alone so bitbake's warm-daemon reconnect keeps
       working; anything else (dead, reused, or the lock is still
       mid-startup) is handled as below.
    4. The marker is absent/garbled, the lock is PRESENT, and the TOPDIR is
       on a shared or unverifiable filesystem -> refuse (``unattributable``).
    5. The marker is absent/garbled, the lock is PRESENT, and the TOPDIR is
       CONFIRMED local -> today's PID probe, file-effect identical to
       before this change, except a live bitbake PID WITH worker/client
       activity now also refuses (``held-locally``) instead of silently
       doing nothing; a live but IDLE bitbake PID (bare cookerdaemon, no
       activity) is left alone so bitbake's warm-daemon reconnect keeps
       working (see :func:`_lock_holder_has_activity`).
    6. The marker is absent/garbled, the lock is ABSENT, and the TOPDIR is
       CONFIRMED local -> today's unconditional orphan-socket removal.
    7. The marker is absent/garbled, the lock is ABSENT, and the TOPDIR is
       shared/unverifiable -> nothing is removed (absence never justifies
       deletion on a shared filesystem); any leftover sockets are reported
       informationally via ``note``.
    """
    build_dir = cfg.bsp_root / cfg.build_dir_name
    lock = build_dir / "bitbake.lock"
    sockets = [build_dir / "bitbake.sock", build_dir / "hashserve.sock"]

    def _remove_all() -> list[Path]:
        removed = []
        for p in [lock, *sockets]:
            if p.exists() or p.is_socket():
                p.unlink(missing_ok=True)
                removed.append(p)
        return removed

    def _remove_own_marker() -> None:
        build_stop.lock_marker_path(cfg).unlink(missing_ok=True)

    owner = build_stop.read_marker_owner(cfg)
    local_host = socket.gethostname()

    if owner is not None and owner != local_host:
        # Row 1: foreign marker - touch NOTHING.
        return build_stop.LockClearOutcome(
            removed=[],
            refusal=build_stop.LockRefusal(
                reason="peer-held",
                host=owner,
                detail=f"lock marker names {escape(owner)}",
                marker=build_stop.lock_marker_path(cfg),
            ),
        )

    if owner is not None:
        # owner == local_host.
        if not lock.exists():
            # Row 2.
            removed = _remove_all()
            _remove_own_marker()
            return build_stop.LockClearOutcome(removed=removed)
        # Row 3.
        pid = proc_tree.read_lock_pid(lock)
        if pid is None:
            # Lock is mid-startup - this node's own build. Leave it intact.
            return build_stop.LockClearOutcome(removed=[])
        if _lock_pid_is_live_bitbake(pid):
            if _lock_holder_has_activity(build_dir):
                return build_stop.LockClearOutcome(
                    removed=[], refusal=build_stop.LockRefusal(reason="held-locally", pid=pid)
                )
            # Live but idle (bare cookerdaemon, no worker/client activity) - the
            # server still owns this lock; leave it for bitbake's reconnect.
            return build_stop.LockClearOutcome(removed=[])
        removed = _remove_all()
        _remove_own_marker()
        return build_stop.LockClearOutcome(removed=removed)

    # owner is None: marker absent or garbled.
    nfs = mounts.is_path_on_nfs(build_dir)
    shared_or_unknown = nfs is not False  # True (nfs) or None (unverifiable) both fail closed.

    if lock.exists():
        if shared_or_unknown:
            # Row 4.
            return build_stop.LockClearOutcome(
                removed=[],
                refusal=build_stop.LockRefusal(
                    reason="unattributable",
                    detail="lock present, no reliable owner, shared/unverifiable filesystem",
                    marker=build_stop.lock_marker_path(cfg),
                ),
            )
        # Row 5: confirmed-local, today's probe.
        pid = proc_tree.read_lock_pid(lock)
        if pid is None:
            # Mid-startup lock on a confirmed-local fs - leave it intact.
            return build_stop.LockClearOutcome(removed=[])
        if _lock_pid_is_live_bitbake(pid):
            if _lock_holder_has_activity(build_dir):
                return build_stop.LockClearOutcome(
                    removed=[], refusal=build_stop.LockRefusal(reason="held-locally", pid=pid)
                )
            # Live but idle (bare cookerdaemon, no worker/client activity) - the
            # server still owns this lock; leave it for bitbake's reconnect.
            return build_stop.LockClearOutcome(removed=[])
        removed = _remove_all()
        return build_stop.LockClearOutcome(removed=removed)

    if not shared_or_unknown:
        # Row 6: confirmed-local, lock absent - today's unconditional orphan-socket removal.
        removed = _remove_all()
        return build_stop.LockClearOutcome(removed=removed)

    # Row 7: shared/unverifiable, lock absent - absence never justifies deletion here.
    leftover = [p for p in sockets if p.exists() or p.is_socket()]
    note = (
        f"leftover sockets present but not removed (shared/unverifiable filesystem): "
        f"{', '.join(str(p) for p in leftover)}"
        if leftover
        else ""
    )
    return build_stop.LockClearOutcome(removed=[], note=note)


class LockHeldByPeerError(Exception):
    """Raised by :func:`lock_owner_marker` when a peer host holds the ownership marker."""

    def __init__(self, host: str, marker: Path | None = None) -> None:
        self.host = host
        self.marker = marker
        super().__init__(f"bitbake lock owned by peer host {host!r}")


class LockUnattributableError(LockHeldByPeerError):
    """The marker could not be attributed while a lock is present on a shared filesystem.

    A subclass of :class:`LockHeldByPeerError` so the acquirer call sites in
    :mod:`bakar.steps.kas_build` keep refusing before launch. ``host`` is a
    placeholder, since no host could be read.
    """

    def __init__(self, marker: Path) -> None:
        super().__init__("unknown (marker unreadable)", marker=marker)
        self.args = (
            f"bitbake lock marker {marker} could not be attributed (unreadable or empty) while bitbake.lock is "
            f"present on a shared or unverifiable filesystem; if no build is running on any node, "
            f"delete {marker} and bitbake.lock manually and retry",
        )


class LockMarkerUnsupportedError(LockUnattributableError):
    """The ownership marker could not be published because ``os.link`` failed.

    Subclasses :class:`LockUnattributableError` so the acquirer call sites in
    :mod:`bakar.steps.kas_build` refuse before launch and print the message
    without a traceback. The marker is published by hard link so no reader ever
    sees it empty; a filesystem without hard links (CIFS without unix
    extensions, some FUSE mounts) cannot offer that, and falling back to an
    exclusive create would expose an empty marker that a second node may
    overwrite. BitBake itself needs hard links in its build directory.
    """

    def __init__(self, marker: Path, cause: OSError) -> None:
        super().__init__(marker)
        text = os.strerror(cause.errno) if cause.errno else str(cause)
        if cause.errno in _HARDLINK_UNSUPPORTED_ERRNOS:
            self.args = (
                f"cannot publish bitbake lock ownership marker {marker}: the build directory's filesystem does not "
                f"support hard links ({text}), which the ownership marker needs; build on a filesystem that does",
            )
        else:
            self.args = (
                f"cannot publish bitbake lock ownership marker {marker}: link failed ({text}) and the marker does "
                f"not name this host; the marker is published by hard link, so retry or build on a filesystem "
                f"that supports hard links; if a stale directory view caused this and no build runs on any node, "
                f"delete the marker {marker} by hand",
            )


class LockMarkerOutcomeUnknownError(LockUnattributableError):
    """Publishing the ownership marker failed and whether it landed could not be determined."""

    def __init__(self, marker: Path) -> None:
        super().__init__(marker)
        self.args = (
            f"the outcome of publishing bitbake lock ownership marker {marker} could not be determined; it may "
            f"name this host, and re-running bakar here clears it; if no build runs on any node, delete the "
            f"marker {marker} by hand",
        )


_MARKER_REREAD_DELAY_S = 0.2
_MARKER_CREATE_ATTEMPTS = 3

# errnos from link(2) meaning the filesystem has no hard links (CIFS without
# unix extensions, some FUSE); EXDEV is deliberately absent, the temp file
# shares the marker's directory. Used only to pick the refusal wording.
_HARDLINK_UNSUPPORTED_ERRNOS = frozenset({errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP})


def _chmod_to_umask_default(path: Path) -> None:
    """Give a mkstemp file (mode 0600) the mode ``open(path, "x")`` would have used.

    A peer node running as another uid must be able to read the published
    marker; an unreadable one is refused as unattributable.
    """
    umask = os.umask(0)
    os.umask(umask)
    path.chmod(0o666 & ~umask)


def _marker_names_host(marker: Path, local_host: str) -> bool:
    """True when ``marker`` exists and holds exactly ``local_host``."""
    try:
        return marker.read_text(encoding="utf-8") == local_host
    except OSError:
        return False


def _link_landed(tmp: Path, marker: Path, local_host: str) -> bool | None:
    """After a failed ``os.link``, report whether the link landed: True, False, or None when unprovable.

    True when the marker names this host, the temp file gained a second name,
    or both paths are the same inode. False when the temp file is readable and
    the marker is absent or another inode. None on any other OSError, so the
    caller refuses with the marker named instead of guessing.
    """
    if _marker_names_host(marker, local_host):
        return True
    try:
        tmp_stat = os.stat(tmp)
        if tmp_stat.st_nlink >= 2:
            return True
        try:
            marker_stat = os.stat(marker)
        except FileNotFoundError:
            return False
        return os.path.samestat(tmp_stat, marker_stat)
    except OSError:
        return None


def _create_marker_exclusive(marker: Path, local_host: str) -> bool:
    """Create ``marker`` holding ``local_host`` in one step; False when it already exists.

    The content is written to a temp file in the marker's directory and
    hard-linked to the marker path, so no reader on any node ever sees an
    empty marker. ``os.link`` fails with ``FileExistsError`` when the target
    exists. Any other ``OSError`` may be the NFS lost-reply case where the link
    succeeded but the reply did not arrive: the marker then already holds
    ``local_host`` and counts as created. Otherwise the failure raises
    :class:`LockMarkerUnsupportedError` (hard links unsupported, or an I/O
    error): the marker is never published by any path that exposes it empty.
    """
    fd, tmp_name = tempfile.mkstemp(dir=str(marker.parent), prefix=f".{marker.name}.")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(local_host)
        _chmod_to_umask_default(tmp)
        try:
            os.link(tmp, marker)
        except FileExistsError:
            return False
        except OSError as exc:
            landed = _link_landed(tmp, marker, local_host)
            if landed:
                return True
            if landed is None:
                raise LockMarkerOutcomeUnknownError(marker) from exc
            raise LockMarkerUnsupportedError(marker, exc) from exc
        return True
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)


def _replace_marker(marker: Path, local_host: str) -> None:
    """Atomically overwrite ``marker`` with ``local_host`` (temp file + ``os.replace``)."""
    fd, tmp_name = tempfile.mkstemp(dir=str(marker.parent), prefix=f".{marker.name}.")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(local_host)
        _chmod_to_umask_default(tmp)
        try:
            os.replace(tmp, marker)
        except OSError as exc:
            raise LockMarkerOutcomeUnknownError(marker) from exc
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise


def _claim_existing_marker(cfg: BuildConfig, marker: Path, local_host: str) -> None:
    """Take over an ownership marker that already exists, or refuse.

    Every decision comes from one :func:`build_stop.read_marker` classification
    per read, so the torn/garbled rules live in one place. Returns once the
    marker holds ``local_host``; raises when it cannot be attributed to this host.

    - A different host's name -> :class:`LockHeldByPeerError`.
    - This host's name -> replaced.
    - Readable but garbled -> replaced: a peer publishes by hard link, so a
      live peer's marker is never garbled; only a crash leaves one.
    - Unreadable or empty, still so after one re-read -> the lock gate is
      consulted once and ANY refusal is honoured; only a ``None`` verdict
      (confirmed-local filesystem) replaces. On a network or unverifiable
      build directory the marker may belong to a live peer whose write is not
      yet visible, so it is never overwritten.
    - Absent (it vanished since the failed create) -> the exclusive create is
      retried, up to ``_MARKER_CREATE_ATTEMPTS`` times, and never replaced
      non-exclusively; still not created -> :class:`LockUnattributableError`.
    """
    for _attempt in range(_MARKER_CREATE_ATTEMPTS):
        state, owner = build_stop.read_marker(cfg)
        if state in ("unreadable", "empty"):
            time.sleep(_MARKER_REREAD_DELAY_S)
            state, owner = build_stop.read_marker(cfg)
        if state == "absent":
            if _create_marker_exclusive(marker, local_host):
                return
            continue
        if state == "owner" and owner is not None and owner != local_host:
            raise LockHeldByPeerError(owner, marker=marker)
        if state in ("unreadable", "empty"):
            refusal = build_stop.lock_mutation_guard(cfg)
            if refusal is not None:
                if refusal.reason == "peer-held" and refusal.host:
                    raise LockHeldByPeerError(refusal.host, marker=marker)
                raise LockUnattributableError(marker)
        _replace_marker(marker, local_host)
        return
    raise LockUnattributableError(marker)


def peer_lock_reason(exc: LockHeldByPeerError) -> str:
    """Failure reason for a peer-held bitbake lock.

    An unattributable marker has only a placeholder host, so its own message is
    logged: it names the marker path and the manual clear. A plain peer reason
    appends the manual-clear hint when the refusal carries its marker. Both
    embed a build-directory path, so they go through :func:`bakar.fmt.neutralize`,
    which strips control characters as well as escaping markup.
    """
    if isinstance(exc, LockUnattributableError):
        return neutralize(exc)
    reason = f"bitbake lock claimed by peer host {neutralize(exc.host)} during acquire; aborting before launch"
    if exc.marker is not None:
        reason += f" {neutralize(build_stop.manual_clear_hint(exc.marker))}"
    return reason


@contextmanager
def lock_owner_marker(cfg: BuildConfig, log: RunLogger) -> Iterator[None]:
    """Claim the TOPDIR's ownership marker for the duration of one bitbake launch.

    On enter: atomically create the marker with its full content (temp file +
    ``os.link``, see :func:`_create_marker_exclusive`) recording this node's
    hostname. If the marker already exists, :func:`_claim_existing_marker`
    decides: a FOREIGN owner raises :class:`LockHeldByPeerError` without
    entering the ``with`` body (the caller must not launch bitbake); an OWN or
    readable-but-GARBLED marker is overwritten atomically and the launch
    proceeds. A marker that is unreadable or empty twice (0.2 s apart) is
    attributed by the lock gate, and any refusal it returns raises: only a
    confirmed-local filesystem lets the launch overwrite it.

    On exit: the marker is removed IFF ``bitbake.lock`` is absent at that
    point (read fresh, never cached) AND the marker still reads exactly this
    node's hostname - this is NEVER gated on the launch's return code or on
    any exception. A marker a peer rewrote meanwhile, or one that cannot be
    read, belongs to someone else and is left alone. If the lock is still
    present (e.g. the launch was SIGKILLed and bitbake never got to clean
    up), the marker is left in place so the next run's row-3 recovery in
    :func:`clear_stale_bitbake_locks` can reclaim it.
    """
    marker = build_stop.lock_marker_path(cfg)
    marker.parent.mkdir(parents=True, exist_ok=True)
    local_host = socket.gethostname()
    if not _create_marker_exclusive(marker, local_host):
        _claim_existing_marker(cfg, marker, local_host)
    try:
        yield
    finally:
        lock = cfg.bsp_root / cfg.build_dir_name / "bitbake.lock"
        if not lock.exists() and _marker_names_host(marker, local_host):
            try:
                marker.unlink(missing_ok=True)
            except OSError as exc:
                log.warn(f"could not remove bitbake lock ownership marker {marker}: {exc}")


def _lock_refusal_message(refusal: build_stop.LockRefusal) -> str:
    """Markup-escaped, human-readable description of a lock refusal for logging."""
    hint = f" {escape(build_stop.manual_clear_hint(refusal.marker))}" if refusal.marker is not None else ""
    if refusal.reason == "peer-held":
        host = escape(refusal.host) if refusal.host else "another host"
        return f"bitbake lock held by peer host {host}; refusing to start.{hint}"
    if refusal.reason == "held-locally":
        pid = refusal.pid if refusal.pid is not None else "unknown"
        return f"bitbake lock held locally by live process pid {pid}; refusing to start"
    if refusal.reason == "unattributable":
        return (
            f"bitbake lock present with no reliable owner on a shared/unverifiable filesystem; refusing to start.{hint}"
        )
    detail = escape(refusal.detail) if refusal.detail else refusal.reason
    return f"bitbake lock refusal ({escape(refusal.reason)}): {detail}"
