"""Exclusive advisory lock serialising every writer of one feed root.

Two overlapping ``bakar feed sync`` runs of one build minted the same snapshot
id, and one died because the render step writes a fixed temp name inside the
repository it is rendering. Nothing in the writers is safe to interleave, so
they take this lock for the whole of their write sequence. Readers (``status``,
``serve``) never take it: the feed is published by atomic pointer swap, so a
read during a write already sees either the old or the new snapshot.

The lock is a POSIX byte-range lock (``fcntl.lockf``) on
``<feed root>/.bakar-feed.lock``. The kernel drops it when the holding process
dies, including on SIGKILL, so a crashed writer cannot leave the feed locked.
The lock file itself is never removed: deleting it would let a second process
lock a fresh inode while the first still holds the old one.

It is POSIX, not ``flock``, because the feed root is one directory that the
main node writes locally and other hosts write over NFS. An NFS client sends
both ``flock`` and ``lockf`` to the server as POSIX byte-range locks, and on the
server's own filesystem ``flock`` and POSIX locks never conflict. Measured on a
pc1 (server) and pc2 (client) pair: a server-local ``flock`` holder did not
exclude a client ``flock`` writer, while POSIX locks on both sides excluded each
other. A host still running the ``flock`` build is therefore not excluded from a
POSIX writer on another host, so every host that writes the feed must run this
build.

POSIX locks belong to the process, not the descriptor: closing any descriptor of
the lock file drops the lock. Only this module opens the file, and it holds its
descriptor for the whole of the body.

Reentrant within a process, because a second ``lockf`` of the same file by the
same process succeeds silently and its release would drop the outer hold: a
writer that calls another writer (a command that also goes through
``sync_then_index``) must not take a second hold, so nested calls skip locking.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import math
import os
import sys
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

LOCK_NAME = ".bakar-feed.lock"
TIMEOUT_ENV = "BAKAR_FEED_LOCK_TIMEOUT"
DEFAULT_TIMEOUT = 900.0
_POLL_SECONDS = 0.1

_held: set[Path] = set()


class FeedLockTimeout(Exception):  # noqa: N818 - a refusal to wait, not a fault
    """The feed lock stayed held by another process for the whole timeout."""


def lock_path(feed_root: Path) -> Path:
    return feed_root / LOCK_NAME


def _timeout() -> float:
    raw = os.environ.get(TIMEOUT_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_TIMEOUT
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{TIMEOUT_ENV} must be a number of seconds, got {raw!r}") from None
    # nan compares false against everything and inf never elapses, so either
    # would make the waiter block forever instead of refusing.
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{TIMEOUT_ENV} must be a finite, non-negative number of seconds, got {raw!r}")
    return value


def validate_timeout() -> None:
    """Raise ``ValueError`` when ``BAKAR_FEED_LOCK_TIMEOUT`` is malformed, so a caller can refuse early."""
    _timeout()


@contextlib.contextmanager
def feed_lock(feed_root: Path) -> Iterator[None]:
    """Hold the exclusive lock on ``feed_root`` for the body.

    Creates the feed root and the lock file when missing. A feed root that
    cannot be written raises the underlying ``OSError`` rather than running
    unlocked. Raises :class:`FeedLockTimeout` after ``BAKAR_FEED_LOCK_TIMEOUT``
    seconds (default 900) without having run the body.
    """
    key = feed_root.resolve()
    if key in _held:
        yield
        return

    timeout = _timeout()
    feed_root.mkdir(parents=True, exist_ok=True)
    path = lock_path(feed_root)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o666)
    try:
        deadline = time.monotonic() + timeout
        announced = False
        while True:
            try:
                fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EACCES):
                    raise
                if time.monotonic() >= deadline:
                    raise FeedLockTimeout(
                        f"feed {feed_root} is locked by another bakar process; gave up on {path} "
                        f"after {timeout:g}s (raise {TIMEOUT_ENV} to wait longer). Nothing was written."
                    ) from None
                if not announced:
                    print(f"waiting for the feed lock {path}", file=sys.stderr, flush=True)
                    announced = True
                time.sleep(_POLL_SECONDS)
        _held.add(key)
        try:
            yield
        finally:
            _held.discard(key)
    finally:
        os.close(fd)
