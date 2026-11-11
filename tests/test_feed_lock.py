"""Tests for the exclusive feed lock held across every feed writer.

Two overlapping syncs of one build once minted the same snapshot id and one
died on a shared temp file. The lock serialises writers; ``status`` and
``serve`` only read and never take it. Real processes are used where the
property is about another process (waiting, SIGKILL release); the feed work
itself is stubbed.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
from typing import TYPE_CHECKING
from unittest import mock

import pytest
from typer.testing import CliRunner

from bakar import feed as feed_mod
from bakar import feed_lock, feed_ops
from bakar.commands import app

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

pytestmark = pytest.mark.unit

_HOLDER = """
import sys, time, pathlib
from bakar import feed_lock
root, ready, release, done = (pathlib.Path(a) for a in sys.argv[1:5])
with feed_lock.feed_lock(root):
    ready.write_text("held")
    while not release.exists():
        time.sleep(0.02)
done.write_text("done")
"""

_WAITER = """
import sys, pathlib
from bakar import feed_lock
root, got = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
with feed_lock.feed_lock(root):
    got.write_text("acquired")
"""


def _wait_for(path: Path, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {path}")
        time.sleep(0.02)


def _spawn(code: str, args: list[object], log: Path, env_extra: dict[str, str] | None = None) -> subprocess.Popen:
    env = {**os.environ, **(env_extra or {})}
    with log.open("wb") as out:
        return subprocess.Popen(
            [sys.executable, "-c", code, *map(str, args)],
            stdout=out,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
        )


def _reap(proc: subprocess.Popen) -> None:
    """Kill a leftover child's own process group, never anything else."""
    if proc.poll() is None:
        with contextlib.suppress(ProcessLookupError):
            if os.getpgid(proc.pid) == proc.pid:
                os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10)


@pytest.fixture
def feed_root(tmp_path: Path) -> Path:
    return tmp_path / "feed"


@pytest.fixture
def procs() -> Iterator[list[subprocess.Popen]]:
    spawned: list[subprocess.Popen] = []
    yield spawned
    for proc in spawned:
        _reap(proc)


@pytest.fixture
def held(feed_root: Path, tmp_path: Path, procs: list[subprocess.Popen]) -> Iterator[Path]:
    """A real process holding the lock until the yielded release file appears."""
    ready, release, done = tmp_path / "ready", tmp_path / "release", tmp_path / "done"
    holder = _spawn(_HOLDER, [feed_root, ready, release, done], tmp_path / "holder.log")
    procs.append(holder)
    _wait_for(ready)
    yield release
    release.write_text("go")


# --- real processes ----------------------------------------------------------


def test_second_process_waits_until_first_releases(feed_root: Path, tmp_path: Path, procs: list) -> None:
    ready, release, done, got = (tmp_path / n for n in ("ready", "release", "done", "got"))
    holder = _spawn(_HOLDER, [feed_root, ready, release, done], tmp_path / "holder.log")
    procs.append(holder)
    _wait_for(ready)

    waiter = _spawn(_WAITER, [feed_root, got], tmp_path / "waiter.log", {"BAKAR_FEED_LOCK_TIMEOUT": "30"})
    procs.append(waiter)
    _wait_for(tmp_path / "waiter.log")  # the one-time notice proves it is blocked, not slow
    time.sleep(0.3)
    assert not got.exists()
    assert waiter.poll() is None

    release.write_text("go")
    assert waiter.wait(timeout=20) == 0
    assert holder.wait(timeout=20) == 0
    assert got.read_text() == "acquired"
    log = (tmp_path / "waiter.log").read_text()
    assert log.count("waiting for the feed lock") == 1
    assert str(feed_root / ".bakar-feed.lock") in log


def test_killed_holder_frees_the_lock(feed_root: Path, held: Path, procs: list) -> None:
    holder = procs[0]
    os.killpg(holder.pid, signal.SIGKILL)
    holder.wait(timeout=10)

    with mock.patch.dict(os.environ, {"BAKAR_FEED_LOCK_TIMEOUT": "5"}), feed_lock.feed_lock(feed_root):
        pass


def test_acquire_times_out_without_writing(feed_root: Path, held: Path) -> None:
    ran = False
    with (
        mock.patch.dict(os.environ, {"BAKAR_FEED_LOCK_TIMEOUT": "0.5"}),
        pytest.raises(feed_lock.FeedLockTimeout) as excinfo,
        feed_lock.feed_lock(feed_root),
    ):
        ran = True
    assert not ran
    assert str(feed_root / ".bakar-feed.lock") in str(excinfo.value)
    assert str(feed_root) in str(excinfo.value)


def test_lock_creates_missing_feed_root_and_lock_file(feed_root: Path) -> None:
    assert not feed_root.exists()
    with feed_lock.feed_lock(feed_root):
        assert (feed_root / ".bakar-feed.lock").is_file()


def test_unwritable_feed_root_fails_loudly(tmp_path: Path) -> None:
    root = tmp_path / "ro"
    root.mkdir()
    root.chmod(0o500)
    try:
        if os.access(root, os.W_OK):
            pytest.skip("running with privileges that ignore directory modes")
        with pytest.raises(OSError, match=r"\.bakar-feed\.lock|Permission"), feed_lock.feed_lock(root):
            pytest.fail("lock body ran on an unwritable feed root")
    finally:
        root.chmod(0o700)


def test_lock_is_reentrant_within_a_process(feed_root: Path) -> None:
    with mock.patch.dict(os.environ, {"BAKAR_FEED_LOCK_TIMEOUT": "1"}):
        with feed_lock.feed_lock(feed_root), feed_lock.feed_lock(feed_root):
            pass
        with feed_lock.feed_lock(feed_root):
            pass


# --- commands ----------------------------------------------------------------


@pytest.fixture
def cli() -> CliRunner:
    return CliRunner()


@pytest.fixture
def cfg(tmp_path: Path, feed_root: Path):
    (feed_root / feed_mod.DEFAULT_RELEASE / feed_mod.DEFAULT_CHANNEL).mkdir(parents=True)
    return mock.Mock(
        effective_feed_dir=feed_root,
        resolved_tmpdir=tmp_path / "build" / "tmp",
        workspace=tmp_path,
    )


def _recording_lock(events: list[str]):
    @contextlib.contextmanager
    def fake(root):
        events.append("enter")
        try:
            yield
        finally:
            events.append("exit")

    return fake


def _sync_result(root: Path) -> dict:
    return {
        "snapshot": "20261110T000000Z",
        "channel_root": root / feed_mod.DEFAULT_RELEASE / feed_mod.DEFAULT_CHANNEL,
        "repos": [],
        "declared": [],
        "unstaged": [],
        "machines": [],
        "pointers": [],
    }


def _plan_stub(events: list[str]):
    plan = mock.Mock(
        unreadable_pointers=[],
        foreign_snapshot_entries=[],
        pool_reclaim_suppressed=False,
        is_empty=True,
        kept_snapshots=[],
    )

    def planned(*a, **k):
        events.append("work")
        return plan

    return planned


def test_each_writer_command_takes_the_lock_around_its_work(
    cli: CliRunner, cfg, tmp_path: Path, feed_root: Path
) -> None:
    events: list[str] = []
    yml = tmp_path / "m.yml"
    yml.write_text("header: {}\n")
    chan = feed_root / feed_mod.DEFAULT_RELEASE / feed_mod.DEFAULT_CHANNEL

    def work(*a, **k):
        events.append("work")
        return _sync_result(feed_root)

    mirrored = mock.Mock(
        feed_root=feed_root,
        source_channel="x",
        repos=[],
        unpublished=[],
        packages_downloaded=0,
        bytes_downloaded=0,
        packages_reused=0,
    )

    def mirror_work(*a, **k):
        events.append("work")
        return mirrored

    def index_work(*a, **k):
        events.append("work")
        return chan / "targets.json"

    scripts = tmp_path / "scripts"
    scripts.mkdir()
    invocations = {
        "sync": (
            ["feed", "sync", str(yml), "--release", "2026-wrynose", "--channel", "local"],
            "bakar.commands.feed.feed_mod.sync",
            work,
        ),
        "mirror": (
            ["feed", "mirror", "http://h/x", "--target", "t"],
            "bakar.commands.feed.feed_mirror.mirror",
            mirror_work,
        ),
        "index": (["feed", "index"], "bakar.commands.feed.feed_index.write_targets_index", index_work),
        "gc": (["feed", "gc"], "bakar.commands.feed.feed_retention.plan_retention", _plan_stub(events)),
    }
    for name, (argv, target, stub) in invocations.items():
        events.clear()
        with (
            mock.patch("bakar.commands.feed._resolve_daemon_cfg", return_value=cfg),
            mock.patch("bakar.feed_lock.feed_lock", _recording_lock(events)),
            mock.patch("bakar.commands.feed.feed_ops.preflight_results", return_value=[]),
            mock.patch("bakar.commands.feed.feed_ops.scripts_dir", return_value=scripts),
            mock.patch("bakar.commands.feed.feed_ops.deploy_dir", return_value=tmp_path),
            mock.patch("bakar.commands.feed._default_release_channel", return_value=("2026-wrynose", "local")),
            mock.patch("bakar.commands.feed.feed_index.derive_targets", return_value={}),
            mock.patch(target, side_effect=stub),
        ):
            result = cli.invoke(app, argv)
        assert result.exit_code == 0, f"{name}: {result.output}"
        assert events == ["enter", "work", "exit"], f"{name}: {events}"


def test_sync_then_index_takes_the_lock_once_around_both_steps(cfg, tmp_path: Path, feed_root: Path) -> None:
    events: list[str] = []

    def synced(*a, **k):
        events.append("stage")
        return _sync_result(feed_root)

    def indexed(*a, **k):
        events.append("index")
        return feed_root / "targets.json"

    with (
        mock.patch("bakar.feed_lock.feed_lock", _recording_lock(events)),
        mock.patch("bakar.feed_ops.feed.sync", side_effect=synced),
        mock.patch("bakar.feed_ops.feed.meta_avocado_scripts", return_value=tmp_path),
        mock.patch("bakar.feed_ops.feed_index.write_targets_index", side_effect=indexed),
    ):
        feed_ops.sync_then_index(cfg, tmp_path / "m.yml")

    assert events == ["enter", "stage", "index", "exit"]


def test_writer_command_exits_nonzero_on_timeout_and_writes_nothing(
    cli: CliRunner, cfg, held: Path, feed_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BAKAR_FEED_LOCK_TIMEOUT", "0.5")
    with (
        mock.patch("bakar.commands.feed._resolve_daemon_cfg", return_value=cfg),
        mock.patch("bakar.commands.feed.feed_retention.plan_retention") as planned,
        mock.patch("bakar.commands.feed.feed_retention.apply_retention") as applied,
    ):
        result = cli.invoke(app, ["feed", "gc", "--confirm"])

    assert result.exit_code != 0
    assert str(feed_root / ".bakar-feed.lock") in result.output.replace("\n", "")
    planned.assert_not_called()
    applied.assert_not_called()


@pytest.mark.parametrize("raw", ["15m", "-1", "nan", "inf"])
def test_writer_command_rejects_a_bad_timeout_cleanly(
    cli: CliRunner, cfg, feed_root: Path, monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    """A bad BAKAR_FEED_LOCK_TIMEOUT is a clean exit 1 naming the variable, never a traceback or an endless wait."""
    monkeypatch.setenv("BAKAR_FEED_LOCK_TIMEOUT", raw)
    with (
        mock.patch("bakar.commands.feed._resolve_daemon_cfg", return_value=cfg),
        mock.patch("bakar.commands.feed.feed_retention.plan_retention") as planned,
        mock.patch("bakar.commands.feed.feed_retention.apply_retention") as applied,
    ):
        result = cli.invoke(app, ["feed", "gc", "--confirm"])

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert "BAKAR_FEED_LOCK_TIMEOUT" in result.output.replace("\n", "")
    planned.assert_not_called()
    applied.assert_not_called()


def test_status_runs_while_the_lock_is_held(cli: CliRunner, cfg, held: Path, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BAKAR_FEED_LOCK_TIMEOUT", "0.5")
    started = time.monotonic()
    with (
        mock.patch("bakar.commands.feed._resolve_daemon_cfg", return_value=cfg),
        mock.patch("bakar.commands.feed.feed_mod.resolve_stage_root", return_value=tmp_path / "no-stage"),
    ):
        result = cli.invoke(app, ["feed", "status"])

    assert result.exit_code == 0, result.output
    assert "waiting for the feed lock" not in result.output
    assert time.monotonic() - started < 0.45
