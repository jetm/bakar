"""Tests for the persistent cache push queue."""

from __future__ import annotations

import sqlite3
import stat

import pytest

from bakar.steps.cache_queue import PushQueue, state_dir


@pytest.fixture
def queue(tmp_path):
    q = PushQueue(tmp_path)
    yield q
    q.close()


def test_enqueue_deduplicates(queue):
    assert queue.enqueue(["a", "b"], now=1.0) == 2
    assert queue.enqueue(["a", "c"], now=2.0) == 1
    assert queue.snapshot(now=3.0).length == 3


def test_pending_oldest_first(queue):
    queue.enqueue(["late"], now=20.0)
    queue.enqueue(["early"], now=10.0)
    assert queue.pending() == ["early", "late"]
    assert queue.pending(limit=1) == ["early"]


def test_remove_deletes(queue):
    queue.enqueue(["a", "b"], now=1.0)
    queue.remove(["a"])
    assert queue.pending() == ["b"]


def test_record_failure_keeps_entry(queue):
    queue.enqueue(["a"], now=1.0)
    queue.record_failure(["a"], "boom")
    row = queue._conn.execute("SELECT attempts, last_error FROM entries").fetchone()
    assert row == (1, "boom")


def test_snapshot_age_uses_injected_now(queue):
    assert queue.snapshot(now=100.0).oldest_age_s is None
    queue.enqueue(["a"], now=40.0)
    assert queue.snapshot(now=100.0).oldest_age_s == 60.0


def test_state_survives_reopen(tmp_path):
    q = PushQueue(tmp_path)
    q.enqueue(["a"], now=1.0)
    q.set_paused("hub down")
    q.mark_reconciled(5.0)
    q.close()
    q2 = PushQueue(tmp_path)
    snap = q2.snapshot(now=9.0)
    q2.close()
    assert snap.length == 1
    assert snap.paused is True
    assert snap.paused_reason == "hub down"
    assert snap.last_reconcile == 5.0


def test_set_paused_none_clears(queue):
    queue.set_paused("x")
    queue.set_paused(None)
    snap = queue.snapshot()
    assert snap.paused is False
    assert snap.paused_reason is None


@pytest.mark.parametrize("bad", ["git2/x", ".hidden", ""])
def test_enqueue_rejects_bad_names(queue, bad):
    with pytest.raises(ValueError):
        queue.enqueue([bad])
    assert queue.snapshot().length == 0


def test_state_dir_honours_xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    d = state_dir()
    assert d == tmp_path / "bakar" / "cache-push"
    assert stat.S_IMODE(d.stat().st_mode) == 0o700


def test_state_dir_ignores_relative_xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", "relative/path")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert state_dir() == tmp_path / ".local" / "state" / "bakar" / "cache-push"


def test_failed_entries_rotate_behind_fresh_ones(queue):
    queue.enqueue(["bad", "good1", "good2"], now=1.0)
    queue.record_failure(["bad"], "boom")
    assert queue.pending(2) == ["good1", "good2"]
    assert queue.pending()[-1] == "bad"


def test_write_rolls_back_on_error(queue):
    queue.enqueue(["a"], now=1.0)
    with pytest.raises(sqlite3.Error), queue._write() as conn:
        conn.execute("DELETE FROM entries")
        conn.execute("SELECT * FROM no_such_table")
    assert queue.pending() == ["a"]
    assert not queue._conn.in_transaction
    queue.enqueue(["b"], now=2.0)
    assert queue.pending() == ["a", "b"]


def test_init_failure_closes_connection(tmp_path, monkeypatch):
    closed = []
    real = sqlite3.connect

    class Conn:
        def __init__(self):
            self._c = real(tmp_path / "x.db", isolation_level=None)

        def execute(self, *a, **k):
            raise sqlite3.OperationalError("boom")

        def close(self):
            closed.append(True)
            self._c.close()

    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: Conn())
    with pytest.raises(sqlite3.OperationalError):
        PushQueue(tmp_path)
    assert closed == [True]
