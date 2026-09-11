"""Bitbake lock/ownership arbitration for the kas build step.

Split out of :mod:`bakar.steps.kas_build` (task 10.2). Every symbol here is
re-exported at ``bakar.steps.kas_build``, including :func:`clear_stale_bitbake_locks`
which :mod:`bakar.diagnostics` reaches via a function-body deferred import of
``bakar.steps.kas_build`` (not of this module), so the re-export is load-bearing.
"""

from __future__ import annotations

import os
import socket
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from rich.markup import escape

from bakar import build_stop

if TYPE_CHECKING:
    from collections.abc import Iterator

    from bakar.config import BuildConfig
    from bakar.observability import RunLogger


def _parse_lock_pid(lock: Path) -> int | None:
    """Tolerant PID parse mirroring ``build_stop._read_bitbake_server_pid``.

    bitbake creates ``bitbake.lock`` and only writes its PID as a later,
    separate step (bb.server.process), so an empty or unparseable lock is a
    server MID-STARTUP, not a stale leftover. Returns ``None`` for that case
    (missing, unreadable, empty, or non-numeric first token) - callers must
    never treat ``None`` here as license to remove the lock.
    """
    try:
        raw = lock.read_text()
    except OSError:
        return None
    tokens = raw.split()
    if not tokens:
        return None
    try:
        return int(tokens[0])
    except ValueError:
        return None


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
        cmdline = cmdline_path.read_bytes().replace(b"\x00", b" ").decode(errors="replace")
        return "bitbake" in cmdline.lower()
    return True


def _lock_holder_has_activity(build_dir: Path) -> bool:
    """True when the lock holder's process tree has activity beyond a bare idle server.

    bitbake's cookerdaemon can persist after a build finishes (a nonzero
    ``BB_SERVER_TIMEOUT``, or stress-parse's persistent-server mode) so later
    invocations reconnect instead of re-spawning - a documented happy path.
    A bare idle server must NOT trip ``held-locally``, or the very next
    ``bakar build`` on this node would refuse instead of reconnecting.

    Reuses :func:`build_stop._collect_build_pids`' argv-scan machinery: its
    ``all_pids`` is the argv-matched cooker plus PGID members plus their
    transitive ``/proc``-ppid descendants (workers, clients). More than the
    bare cooker PID itself in that set means genuine worker/client activity.

    Defined here, re-exported at :mod:`bakar.steps.kas_build` for
    :mod:`bakar.steps.kas_graph_capture`'s deferred-import call (that module's
    own docstring explains why it must reach collaborators through
    ``kas_build`` rather than by name). A test wanting to fake this predicate
    for BOTH consumers has two separate bindings to patch: this module's own
    calls below resolve against ``kas_lock``'s globals and need
    ``monkeypatch.setattr(kas_lock, "_lock_holder_has_activity", ...)``;
    :func:`kas_graph_capture._wait_for_cooker_idle` needs
    ``monkeypatch.setattr(kas_build, "_lock_holder_has_activity", ...)``
    instead - patching one does not affect the other.
    """
    procs = build_stop._collect_build_pids(build_dir, None)
    return len(procs.all_pids) > 1


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
            refusal=build_stop.LockRefusal(reason="peer-held", host=owner, detail=f"lock marker names {escape(owner)}"),
        )

    if owner is not None:
        # owner == local_host.
        if not lock.exists():
            # Row 2.
            removed = _remove_all()
            _remove_own_marker()
            return build_stop.LockClearOutcome(removed=removed)
        # Row 3.
        pid = _parse_lock_pid(lock)
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
    # Deferred: tests monkeypatch kas_build.is_path_on_nfs (this function is
    # re-exported there) and expect that patch to reach this call.
    from bakar.steps import kas_build

    nfs = kas_build.is_path_on_nfs(build_dir)
    shared_or_unknown = nfs is not False  # True (nfs) or None (unverifiable) both fail closed.

    if lock.exists():
        if shared_or_unknown:
            # Row 4.
            return build_stop.LockClearOutcome(
                removed=[],
                refusal=build_stop.LockRefusal(
                    reason="unattributable",
                    detail="lock present, no reliable owner, shared/unverifiable filesystem",
                ),
            )
        # Row 5: confirmed-local, today's probe.
        pid = _parse_lock_pid(lock)
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

    def __init__(self, host: str) -> None:
        self.host = host
        super().__init__(f"bitbake lock owned by peer host {host!r}")


@contextmanager
def lock_owner_marker(cfg: BuildConfig, log: RunLogger) -> Iterator[None]:
    """Claim the TOPDIR's ownership marker for the duration of one bitbake launch.

    On enter: atomically create the marker (``open(..., "x")`` - O_EXCL,
    atomic even on NFS) recording this node's hostname. If the marker
    already exists: a FOREIGN owner raises :class:`LockHeldByPeerError`
    without entering the ``with`` body (the caller must not launch bitbake);
    an OWN or GARBLED marker is overwritten atomically (temp file +
    ``os.replace`` in the same directory) and the launch proceeds.

    On exit: the marker is removed IFF ``bitbake.lock`` is absent at that
    point (read fresh, never cached) - this is NEVER gated on the launch's
    return code or on any exception. If the lock is still present (e.g. the
    launch was SIGKILLed and bitbake never got to clean up), the marker is
    left in place so the next run's row-3 recovery in
    :func:`clear_stale_bitbake_locks` can reclaim it.
    """
    marker = build_stop.lock_marker_path(cfg)
    marker.parent.mkdir(parents=True, exist_ok=True)
    local_host = socket.gethostname()
    try:
        with open(marker, "x", encoding="utf-8") as fh:
            fh.write(local_host)
    except FileExistsError:
        owner = build_stop.read_marker_owner(cfg)
        if owner is not None and owner != local_host:
            raise LockHeldByPeerError(owner) from None
        fd, tmp_name = tempfile.mkstemp(dir=str(marker.parent), prefix=f".{marker.name}.")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(local_host)
            os.replace(tmp, marker)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    try:
        yield
    finally:
        lock = cfg.bsp_root / cfg.build_dir_name / "bitbake.lock"
        if not lock.exists():
            marker.unlink(missing_ok=True)


def _lock_refusal_message(refusal: build_stop.LockRefusal) -> str:
    """Markup-escaped, human-readable description of a lock refusal for logging."""
    if refusal.reason == "peer-held":
        host = escape(refusal.host) if refusal.host else "another host"
        return f"bitbake lock held by peer host {host}; refusing to start"
    if refusal.reason == "held-locally":
        pid = refusal.pid if refusal.pid is not None else "unknown"
        return f"bitbake lock held locally by live process pid {pid}; refusing to start"
    if refusal.reason == "unattributable":
        return "bitbake lock present with no reliable owner on a shared/unverifiable filesystem; refusing to start"
    detail = escape(refusal.detail) if refusal.detail else refusal.reason
    return f"bitbake lock refusal ({escape(refusal.reason)}): {detail}"
