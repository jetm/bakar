"""Exclusive advisory lock serialising every writer of one feed root.

Two overlapping ``bakar feed sync`` runs of one build minted the same snapshot
id, and one died because the render step writes a fixed temp name inside the
repository it is rendering. Nothing in the writers is safe to interleave, so
they take this lock for the whole of their write sequence. Readers (``status``,
``serve``) never take it: the feed is published by atomic pointer swap, so a
read during a write already sees either the old or the new snapshot.

The lock is ``fcntl.flock`` on ``<feed root>/.bakar-feed.lock``. The kernel
drops it when the holding process dies, including on SIGKILL, so a crashed
writer cannot leave the feed locked. The lock file itself is never removed:
deleting it would let a second process lock a fresh inode while the first still
holds the old one.

Reentrant within a process, because ``flock`` on a second descriptor of the same
file blocks against the first: a writer that calls another writer (a command
that also goes through ``sync_then_index``) would deadlock on itself otherwise.
"""

from __future__ import annotations

import contextlib
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
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
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
