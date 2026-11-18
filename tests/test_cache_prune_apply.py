"""Tests for the removal half of ``bakar.steps.cache_prune`` (tmp_path only)."""

from __future__ import annotations

import fcntl
import os
import signal
import subprocess
import sys
import time
from typing import TYPE_CHECKING

import pytest

from bakar.steps import cache_prune
from bakar.steps.cache_prune import Item, Plan, apply_plan, render_report

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_HOLDER = (
    "import fcntl, os, sys, time\n"
    "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o644)\n"
    "fcntl.flock(fd, fcntl.LOCK_EX)\n"
    "print('ready', flush=True)\n"
    "time.sleep(60)\n"
)


def _key(path: Path) -> tuple[int, int]:
    st = os.lstat(path)
    return (st.st_size, st.st_mtime_ns)


def _hub_item(dl: Path, name: str = "a.tar.gz") -> Item:
    (dl / name).write_bytes(b"x" * 100)
    (dl / (name + ".done")).write_text("")
    return Item("hub", name, (dl / (name + ".done"), dl / name), 100, dl / (name + ".lock"), _key(dl / name))


def _tarball_item(dl: Path, clone: str = "repo") -> Item:
    git2 = dl / "git2"
    (git2 / clone).mkdir(parents=True, exist_ok=True)
    name = f"git2_{clone}.tar.gz"
    (dl / name).write_bytes(b"t" * 50)
    (dl / (name + ".done")).write_text("")
    return Item("tarball", name, (dl / (name + ".done"), dl / name), 50, git2 / (clone + ".lock"), _key(dl / name))


def _clone_item(dl: Path, name: str = "old") -> Item:
    git2 = dl / "git2"
    clone = git2 / name
    clone.mkdir(parents=True, exist_ok=True)
    (clone / "HEAD").write_text("ref")
    (git2 / (name + ".done")).write_text("")
    return Item("clone", name, (git2 / (name + ".done"), clone), 3, git2 / (name + ".lock"), _key(clone))


def _plan(*items: Item) -> Plan:
    plan = Plan()
    for item in items:
        plan.items[item.kind].append(item)
    return plan


@pytest.fixture
def dl(tmp_path: Path) -> Path:
    d = tmp_path / "downloads"
    (d / "git2").mkdir(parents=True)
    return d


@pytest.fixture
def holder_factory(tmp_path: Path) -> Iterator[object]:
    procs: list[tuple[subprocess.Popen[bytes], object]] = []

    def start(lock: Path) -> None:
        out = tmp_path / f"holder{len(procs)}.out"
        fh = out.open("wb")
        child = subprocess.Popen(
            [sys.executable, "-c", _HOLDER, str(lock)],
            stdout=fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        procs.append((child, fh))
        for _ in range(100):
            if out.read_bytes().startswith(b"ready"):
                return
            time.sleep(0.05)
        raise AssertionError("holder never took the lock")

    yield start
    for child, fh in procs:
        if os.getpgid(child.pid) == child.pid:
            os.killpg(child.pid, signal.SIGTERM)
        child.wait(timeout=10)
        fh.close()  # type: ignore[attr-defined]


def test_dry_run_report_changes_nothing(dl: Path) -> None:
    item = _hub_item(dl)
    plan = _plan(item)
    before = sorted(p.name for p in dl.iterdir())
    text = render_report(plan, None, apply=False)
    assert sorted(p.name for p in dl.iterdir()) == before
    assert "would remove 1 item(s), 100 B" in text
    assert "dry run" in text


def test_apply_removes_pair_and_leaves_others(dl: Path) -> None:
    item = _hub_item(dl)
    (dl / "other.bin").write_bytes(b"o")
    (dl / "other.bin.done").write_text("")
    result = apply_plan(_plan(item), dl)
    assert result.removed["hub"] == 1
    assert result.removed_size["hub"] == 100
    assert not (dl / "a.tar.gz").exists()
    assert not (dl / "a.tar.gz.done").exists()
    assert (dl / "other.bin").exists()
    assert (dl / "other.bin.done").exists()


def test_lock_file_gone_after_removal(dl: Path) -> None:
    item = _hub_item(dl)
    apply_plan(_plan(item), dl)
    assert not item.lock.exists()


def test_failure_between_stamp_and_payload_leaves_payload_without_stamp(
    dl: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _hub_item(dl)
    real = cache_prune._remove

    def flaky(path: Path, *, tree: bool = False) -> None:
        if path == item.paths[1]:
            raise OSError("injected")
        real(path, tree=tree)

    monkeypatch.setattr(cache_prune, "_remove", flaky)
    result = apply_plan(_plan(item), dl)
    assert (dl / "a.tar.gz").exists()
    assert not (dl / "a.tar.gz.done").exists()
    assert result.kept["hub", "remove failed"] == 1
    assert result.removed["hub"] == 0


def test_locked_item_is_kept_in_use(dl: Path, holder_factory) -> None:  # type: ignore[no-untyped-def]
    item = _hub_item(dl)
    holder_factory(item.lock)
    result = apply_plan(_plan(item), dl)
    assert (dl / "a.tar.gz").exists()
    assert (dl / "a.tar.gz.done").exists()
    assert result.kept["hub", "in use"] == 1


def test_tarball_kept_when_clone_lock_held(dl: Path, holder_factory) -> None:  # type: ignore[no-untyped-def]
    item = _tarball_item(dl)
    holder_factory(item.lock)
    result = apply_plan(_plan(item), dl)
    assert (dl / "git2_repo.tar.gz").exists()
    assert result.kept["tarball", "in use"] == 1


def test_tarball_removed_when_free(dl: Path) -> None:
    item = _tarball_item(dl)
    result = apply_plan(_plan(item), dl)
    assert result.removed["tarball"] == 1
    assert not (dl / "git2_repo.tar.gz").exists()
    assert (dl / "git2" / "repo").is_dir()
    assert not item.lock.exists()


def test_payload_replaced_after_planning_is_kept(dl: Path) -> None:
    item = _hub_item(dl)
    (dl / "a.tar.gz").write_bytes(b"y" * 7)
    result = apply_plan(_plan(item), dl)
    assert (dl / "a.tar.gz").read_bytes() == b"y" * 7
    assert (dl / "a.tar.gz.done").exists()
    assert result.kept["hub", "changed since planned"] == 1


def test_symlink_swapped_in_is_not_followed(dl: Path, tmp_path: Path) -> None:
    item = _hub_item(dl)
    target = tmp_path / "precious"
    target.write_bytes(b"x" * 100)
    (dl / "a.tar.gz").unlink()
    (dl / "a.tar.gz").symlink_to(target)
    result = apply_plan(_plan(item), dl)
    assert target.read_bytes() == b"x" * 100
    assert (dl / "a.tar.gz").is_symlink()
    assert result.kept["hub", "changed since planned"] == 1


def test_clone_symlink_swapped_in_is_not_followed(dl: Path, tmp_path: Path) -> None:
    item = _clone_item(dl)
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep").write_text("k")
    (dl / "git2" / "old" / "HEAD").unlink()
    (dl / "git2" / "old").rmdir()
    (dl / "git2" / "old").symlink_to(victim)
    result = apply_plan(_plan(item), dl)
    assert (victim / "keep").exists()
    assert result.kept["clone", "changed since planned"] == 1


def test_clone_removed_under_git2(dl: Path) -> None:
    item = _clone_item(dl)
    result = apply_plan(_plan(item), dl)
    assert result.removed["clone"] == 1
    assert not (dl / "git2" / "old").exists()
    assert not (dl / "git2" / "old.done").exists()
    assert not item.lock.exists()


def test_clone_outside_git2_is_kept(dl: Path) -> None:
    outside = dl / "elsewhere"
    outside.mkdir()
    (outside / "f").write_text("f")
    stamp = dl / "elsewhere.done"
    stamp.write_text("")
    item = Item("clone", "elsewhere", (stamp, outside), 1, dl / "elsewhere.lock", _key(outside))
    result = apply_plan(_plan(item), dl)
    assert (outside / "f").exists()
    assert result.kept["clone", "changed since planned"] == 1


def test_report_names_counts_sizes_and_reasons(dl: Path, holder_factory) -> None:  # type: ignore[no-untyped-def]
    free = _hub_item(dl, "free.bin")
    busy = _hub_item(dl, "busy.bin")
    holder_factory(busy.lock)
    plan = _plan(free, busy)
    plan.keep("hub", "size mismatch", 2)
    result = apply_plan(plan, dl)
    text = render_report(plan, result, apply=True)
    assert "hub: examined" in text
    assert "removed 1 item(s), 100 B" in text
    assert "kept 2: size mismatch" in text
    assert "kept 1: in use" in text
    assert "clone: examined" in text
    assert "applied" in text
    assert "dry run" not in text


def test_report_shows_status_for_unavailable_class(dl: Path) -> None:
    plan = Plan()
    plan.status["hub"] = cache_prune.STATUS_UNAVAILABLE
    plan.status["clone"] = cache_prune.STATUS_NOT_REQUESTED
    text = render_report(plan, None, apply=False)
    assert "hub: unavailable" in text
    assert "clone: not requested" in text


def test_flock_is_exclusive_helper_sanity(dl: Path) -> None:
    # The lock must be taken with flock so BitBake's own flock contends with it.
    item = _hub_item(dl)
    fd = os.open(item.lock, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
        result = apply_plan(_plan(item), dl)
    finally:
        os.close(fd)
    assert result.kept["hub", "in use"] == 1
