"""Persistent push queue for the shared download cache.

Every eligible file is recorded here before its transfer starts and removed
only after the transfer succeeds, so a crash, a killed process or an
unreachable hub never loses a file. The database lives in the XDG state
directory, never under the downloads, sstate or ccache directories, which
may be NFS-backed and are not safe homes for SQLite locking.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

_PAUSED_KEY = "paused"
_RECONCILE_KEY = "last_reconcile"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    name TEXT PRIMARY KEY,
    enqueued_at REAL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


@dataclass(frozen=True)
class QueueSnapshot:
    length: int
    oldest_age_s: float | None
    paused: bool
    paused_reason: str | None
    last_reconcile: float | None


def state_dir_path() -> Path:
    """Return the push-queue state directory path without creating anything."""
    xdg = os.environ.get("XDG_STATE_HOME", "")
    if xdg and os.path.isabs(xdg):
        base = Path(xdg)
    else:
        base = Path.home() / ".local" / "state"
    return base / "bakar" / "cache-push"


def state_dir() -> Path:
    """Return the push-queue state directory, creating it with mode 0700."""
    path = state_dir_path()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def queue_exists() -> bool:
    """True when a queue database already exists; creates no file or directory."""
    return (state_dir_path() / "queue.db").is_file()


def has_control_char(name: str) -> bool:
    """True when ``name`` holds an ASCII control character (newline and NUL included)."""
    return any(ord(c) < 0x20 or ord(c) == 0x7F for c in name)


def is_unsafe_name(name: str) -> bool:
    """True for a name that must never be queued or listed for rsync.

    rsync reads a ``--files-from`` entry starting with ``#`` or ``;`` as a comment
    even with ``--from0``, so such a file would be skipped while rsync exits 0.
    A name that is not valid UTF-8 (surrogate-escaped) cannot be stored in SQLite.
    """
    if not name or "/" in name or name.startswith((".", "#", ";")) or has_control_char(name):
        return True
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        return True
    return False


def _check_name(name: str) -> None:
    if is_unsafe_name(name):
        raise ValueError(f"invalid cache entry name: {name!r}")


class PushQueue:
    def __init__(self, directory: Path | None = None) -> None:
        base = directory if directory is not None else state_dir()
        base.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = base / "queue.db"
        self._conn = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        try:
            self._conn.execute("PRAGMA busy_timeout = 5000")
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.executescript(_SCHEMA)
        except BaseException:
            self._conn.close()
            raise

    @contextlib.contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        """Run one BEGIN IMMEDIATE...COMMIT, rolling back if anything fails."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
            self._conn.execute("COMMIT")
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                self._conn.execute("ROLLBACK")
            raise

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> PushQueue:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def enqueue(self, names: list[str], now: float | None = None) -> int:
        """Insert names not already queued; return how many were new."""
        for name in names:
            _check_name(name)
        ts = time.time() if now is None else now
        added = 0
        with self._write() as conn:
            for name in names:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO entries(name, enqueued_at) VALUES (?, ?)",
                    (name, ts),
                )
                added += cur.rowcount
        return added

    def pending(self, limit: int | None = None) -> list[str]:
        # attempts first: an entry that keeps failing rotates behind fresh ones,
        # so one poison file cannot hold a full batch hostage forever.
        sql = "SELECT name FROM entries ORDER BY attempts, enqueued_at, name"
        args: tuple[int, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            args = (limit,)
        return [row[0] for row in self._conn.execute(sql, args)]

    def remove(self, names: list[str]) -> None:
        with self._write() as conn:
            conn.executemany("DELETE FROM entries WHERE name = ?", [(n,) for n in names])

    def record_failure(self, names: list[str], error: str) -> None:
        with self._write() as conn:
            conn.executemany(
                "UPDATE entries SET attempts = attempts + 1, last_error = ? WHERE name = ?",
                [(error, n) for n in names],
            )

    def set_paused(self, reason: str | None) -> None:
        if reason is None:
            self._conn.execute("DELETE FROM meta WHERE key = ?", (_PAUSED_KEY,))
        else:
            self._conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                (_PAUSED_KEY, reason),
            )

    def mark_reconciled(self, now: float) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
            (_RECONCILE_KEY, repr(float(now))),
        )

    def snapshot(self, now: float | None = None) -> QueueSnapshot:
        ts = time.time() if now is None else now
        length, oldest = self._conn.execute("SELECT COUNT(*), MIN(enqueued_at) FROM entries").fetchone()
        meta = dict(self._conn.execute("SELECT key, value FROM meta"))
        paused = _PAUSED_KEY in meta
        reconcile = meta.get(_RECONCILE_KEY)
        return QueueSnapshot(
            length=length,
            oldest_age_s=None if oldest is None else max(0.0, ts - oldest),
            paused=paused,
            paused_reason=(meta[_PAUSED_KEY] or None) if paused else None,
            last_reconcile=None if reconcile is None else float(reconcile),
        )
