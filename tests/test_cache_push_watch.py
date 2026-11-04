"""Watcher, reconcile and backoff loop for the shared download-cache push."""

from __future__ import annotations

import subprocess
import threading
from typing import TYPE_CHECKING, Any

import pytest

from bakar.steps import cache_push
from bakar.steps.cache_push import check_prerequisites, inotify_argv, watch
from bakar.steps.cache_queue import PushQueue

if TYPE_CHECKING:
    from pathlib import Path


class FakeProc:
    def __init__(self, names: list[str]) -> None:
        self.stdout = iter([n + "\n" for n in names])
        self.terminated = False

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: float | None = None) -> int:
        self.waited = True
        return 0

    def kill(self) -> None:
        self.killed = True


class Harness:
    """Injected clock, sleep, popen and runner for one watch() run."""

    def __init__(self, streams: list[list[str]], push_rcs: list[int], reconcile_out: str = "") -> None:
        self.t = 0.0
        self.sleeps: list[float] = []
        self.streams = streams
        self.popen_argvs: list[list[str]] = []
        self.push_rcs = push_rcs
        self.reconcile_out = reconcile_out
        self.pushed: list[list[str]] = []
        self.stop = threading.Event()
        self.steps = 0

    def clock(self) -> float:
        return self.t

    def sleep(self, secs: float) -> None:
        self.sleeps.append(secs)
        self.t += secs
        self.steps += 1
        if self.steps > 5000:
            self.stop.set()

    def popen(self, argv: list[str], **_kw: Any) -> FakeProc:
        self.popen_argvs.append(argv)
        return FakeProc(self.streams.pop(0) if self.streams else [])

    def runner(self, argv: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        if "--dry-run" in argv:
            return subprocess.CompletedProcess(argv, 0, self.reconcile_out, "")
        list_file = next(a for a in argv if a.startswith("--files-from=")).split("=", 1)[1]
        with open(list_file) as fh:
            self.pushed.append(fh.read().split("\0")[:-1])
        rc = self.push_rcs.pop(0) if len(self.push_rcs) > 1 else self.push_rcs[0]
        return subprocess.CompletedProcess(argv, rc, "", "No space left on device" if rc == 11 else "")

    def run(self, dl: Path, queue: PushQueue, **kw: Any) -> None:
        watch(
            dl,
            "hub:/dl",
            dl / "key",
            queue=queue,
            stop_event=self.stop,
            runner=self.runner,
            popen=self.popen,
            clock=self.clock,
            sleep=self.sleep,
            **kw,
        )


def _make(dl: Path, *names: str) -> None:
    for n in names:
        (dl / n).write_text("x")
        (dl / (n + ".done")).write_text("")


@pytest.fixture
def dl(tmp_path: Path) -> Path:
    d = tmp_path / "dl"
    d.mkdir()
    return d


def test_outage_entries_reach_runner_after_recovery(dl: Path, tmp_path: Path) -> None:
    _make(dl, "a.tar.gz", "b.tar.gz")
    h = Harness([["a.tar.gz.done", "b.tar.gz.done"]], [255, 255, 0])
    with PushQueue(tmp_path / "q") as q:
        orig = h.runner

        def runner(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
            res = orig(argv, **kw)
            if not q.pending(1):
                h.stop.set()
            return res

        h.runner = runner  # type: ignore[method-assign]
        h.run(dl, q)
        assert q.pending() == []
    assert len(h.pushed) == 3
    assert sorted(h.pushed[-1]) == ["a.tar.gz", "a.tar.gz.done", "b.tar.gz", "b.tar.gz.done"]


def test_reconcile_enqueues_download_finished_before_startup(dl: Path, tmp_path: Path) -> None:
    _make(dl, "old.tar.gz")
    h = Harness([[]], [0], reconcile_out="old.tar.gz\nold.tar.gz.done\n")
    with PushQueue(tmp_path / "q") as q:
        orig = h.runner

        def runner(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
            res = orig(argv, **kw)
            if "--dry-run" not in argv:
                h.stop.set()
            return res

        h.runner = runner  # type: ignore[method-assign]
        h.run(dl, q)
        assert q.snapshot().last_reconcile is not None
    assert h.pushed == [["old.tar.gz", "old.tar.gz.done"]]


def test_reopening_queue_keeps_entries(dl: Path, tmp_path: Path) -> None:
    _make(dl, "a.tar.gz")
    h = Harness([["a.tar.gz.done"]], [255])
    with PushQueue(tmp_path / "q") as q:
        orig = h.sleep

        def sleep(secs: float) -> None:
            orig(secs)
            if h.pushed:
                h.stop.set()

        h.sleep = sleep  # type: ignore[method-assign]
        h.run(dl, q)
    with PushQueue(tmp_path / "q") as q2:
        assert q2.pending() == ["a.tar.gz", "a.tar.gz.done"]


def test_watch_passes_mirror_url_to_drain(dl: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _make(dl, "a.tar.gz")
    h = Harness([["a.tar.gz.done"]], [0])
    seen: list[str] = []

    def head(url: str, timeout: float) -> int | None:
        seen.append(url)
        return 200

    monkeypatch.setattr(cache_push.probes, "head_status", head)
    with PushQueue(tmp_path / "q") as q:
        h.run(dl, q, mirror_url="http://hub.test")
        assert q.pending() == []
    assert seen == ["http://hub.test/a.tar.gz", "http://hub.test/a.tar.gz.done"]
    assert h.pushed == []


def test_full_disk_keeps_entries_and_retries_after_interval(dl: Path, tmp_path: Path) -> None:
    _make(dl, "a.tar.gz")
    h = Harness([["a.tar.gz.done"]], [11, 0])
    times: list[float] = []
    with PushQueue(tmp_path / "q") as q:
        orig = h.runner

        def runner(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
            res = orig(argv, **kw)
            if "--dry-run" not in argv:
                times.append(h.t)
                if len(times) == 1:
                    assert q.pending() == ["a.tar.gz", "a.tar.gz.done"]
                else:
                    h.stop.set()
            return res

        h.runner = runner  # type: ignore[method-assign]
        h.run(dl, q)
    assert times[1] - times[0] >= 300


def test_inotify_argv_not_recursive(dl: Path) -> None:
    argv = inotify_argv(dl)
    assert "-r" not in argv
    assert "-m" in argv
    assert argv[-1] == str(dl)


def test_watch_launches_non_recursive_inotifywait(dl: Path, tmp_path: Path) -> None:
    h = Harness([[]], [0])
    h.stop.set()
    with PushQueue(tmp_path / "q") as q:
        h.stop.clear()
        orig = h.sleep

        def sleep(secs: float) -> None:
            orig(secs)
            h.stop.set()

        h.sleep = sleep  # type: ignore[method-assign]
        h.run(dl, q)
    assert h.popen_argvs
    assert "-r" not in h.popen_argvs[0]


def test_inotify_exit_restarts_after_five_seconds(dl: Path, tmp_path: Path) -> None:
    h = Harness([[], []], [0])
    with PushQueue(tmp_path / "q") as q:
        orig = h.popen

        def popen(argv: list[str], **kw: Any) -> FakeProc:
            proc = orig(argv, **kw)
            if len(h.popen_argvs) == 2:
                h.stop.set()
            return proc

        h.popen = popen  # type: ignore[method-assign]
        h.run(dl, q)
    assert len(h.popen_argvs) == 2
    assert h.t >= 5


def test_prerequisite_messages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    key = tmp_path / "key"
    dl = tmp_path / "dl"
    monkeypatch.setattr(cache_push.shutil, "which", lambda _n: "/usr/bin/inotifywait")
    assert "cache_push_target" in (check_prerequisites(dl, None, key) or "")
    assert "downloads" in (check_prerequisites(None, "hub:/dl", key) or "")
    assert str(key) in (check_prerequisites(dl, "hub:/dl", key) or "")
    monkeypatch.setattr(cache_push.shutil, "which", lambda _n: None)
    assert "inotifywait" in (check_prerequisites(dl, "hub:/dl", key) or "")
    monkeypatch.setattr(cache_push.shutil, "which", lambda _n: "/usr/bin/inotifywait")
    key.write_text("k")
    assert check_prerequisites(dl, "hub:/dl", key) is None
    assert dl.is_dir()


def test_popen_oserror_backs_off_and_retries(dl: Path, tmp_path: Path) -> None:
    h = Harness([[]], [0])
    attempts: list[float] = []
    orig = h.popen

    def popen(argv: list[str], **kw: Any) -> FakeProc:
        attempts.append(h.t)
        if len(attempts) == 1:
            raise FileNotFoundError("inotifywait")
        h.stop.set()
        return orig(argv, **kw)

    h.popen = popen  # type: ignore[method-assign]
    with PushQueue(tmp_path / "q") as q:
        h.run(dl, q)
    assert len(attempts) == 2
    assert attempts[1] - attempts[0] >= 5


def test_scandir_oserror_does_not_kill_watch(dl: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h = Harness([[]], [0])
    calls = []

    def boom(_dl: Path) -> list[str]:
        calls.append(1)
        h.stop.set()
        raise PermissionError("denied")

    monkeypatch.setattr(cache_push, "scan_candidates", boom)
    with PushQueue(tmp_path / "q") as q:
        h.run(dl, q)
    assert calls


def test_inotify_exit_reaps_process(dl: Path, tmp_path: Path) -> None:
    h = Harness([[], []], [0])
    procs: list[FakeProc] = []
    orig = h.popen

    def popen(argv: list[str], **kw: Any) -> FakeProc:
        proc = orig(argv, **kw)
        procs.append(proc)
        if len(procs) == 2:
            h.stop.set()
        return proc

    h.popen = popen  # type: ignore[method-assign]
    with PushQueue(tmp_path / "q") as q:
        h.run(dl, q)
    assert procs[0].waited
    assert procs[1].waited
