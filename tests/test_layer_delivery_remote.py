"""Tests for the remote worktree delivery script, run with bash on real repos."""

from __future__ import annotations

import fcntl
import json
import os
import shlex
import shutil
import signal
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from bakar.release_workspace import ReleaseRoutingError
from bakar.steps.layer_delivery_remote import (
    EXIT_BUILD_RUNNING,
    EXIT_DIRTY,
    EXIT_FOREIGN_MARKER,
    EXIT_LOCKED,
    EXIT_MOVED,
    EXIT_NO_BUILD_LOCK,
    EXIT_NO_MARKER,
    MARKER_FILENAME,
    DeliveryTarget,
    MarkerFields,
    build_guard,
    build_lock_path,
    describe_exit,
    ensure_worktree_script,
    marker_json,
    parse_marker,
)

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}

HOST = "laptop"
SRC = "/home/me/meta-avocado"
BUILD_DIR = "build-m-wt1"


def _git(*args: str | Path, cwd: Path | None = None) -> str:
    env = {**os.environ, **_GIT_ENV}
    out = subprocess.run(
        ["git", *map(str, args)],
        cwd=cwd,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return out.stdout.strip()


def _commit(repo: Path, filename: str, content: str) -> str:
    (repo / filename).write_text(content)
    _git("-C", repo, "add", filename)
    _git("-C", repo, "commit", "-q", "-m", filename)
    return _git("-C", repo, "rev-parse", "HEAD")


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "peridio wrynose"
    repo = ws / "meta-avocado"
    repo.mkdir(parents=True)
    _git("init", "-q", "-b", "main", repo)
    _commit(repo, "a.txt", "a\n")
    return ws


def _script(workspace: Path, root: Path, commit: str, *, src: str = SRC, aliases: tuple[str, ...] = ()) -> str:
    return ensure_worktree_script(
        DeliveryTarget(workspace=str(workspace), worktree_root=str(root)),
        MarkerFields(source_host=HOST, source_path=src, name="wt1", commit=commit, snapshot=False),
        source_path_aliases=aliases,
    )


def _exec(script: str) -> subprocess.CompletedProcess:
    env = {**os.environ, **_GIT_ENV}
    return subprocess.run(
        ["bash", "-c", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _run(workspace: Path, root: Path, commit: str, *, host: str = HOST, src: str = SRC):
    script = ensure_worktree_script(
        DeliveryTarget(workspace=str(workspace), worktree_root=str(root)),
        MarkerFields(source_host=host, source_path=src, name="wt1", commit=commit, snapshot=False),
    )
    return _exec(script)


def _head(path: Path) -> str:
    return _git("-C", path, "rev-parse", "HEAD")


def _marker(path: Path) -> dict:
    gitdir = Path(_git("-C", path, "rev-parse", "--absolute-git-dir"))
    return parse_marker((gitdir / MARKER_FILENAME).read_text())


def test_fresh_add_creates_root_worktree_and_marker(workspace: Path, tmp_path: Path):
    root = tmp_path / "not" / "yet" / "worktrees"
    commit = _head(workspace / "meta-avocado")
    res = _run(workspace, root, commit)
    assert res.returncode == 0, res.stderr
    dest = root / "wt1"
    assert _head(dest) == commit
    marker = _marker(dest)
    assert marker["source_host"] == HOST
    assert marker["source_path"] == SRC
    assert marker["commit"] == commit
    assert marker["name"] == "wt1"
    assert marker["snapshot"] is False
    assert _git("-C", dest, "status", "--porcelain") == ""
    assert not (workspace / BUILD_DIR).exists()


def test_same_commit_is_noop(workspace: Path, tmp_path: Path):
    root = tmp_path / "wts"
    commit = _head(workspace / "meta-avocado")
    assert _run(workspace, root, commit).returncode == 0
    before = _marker(root / "wt1")
    res = _run(workspace, root, commit)
    assert res.returncode == 0, res.stderr
    assert "already at" in res.stdout
    assert _marker(root / "wt1") == before


def test_new_commit_moves_worktree_without_build_dir(workspace: Path, tmp_path: Path):
    repo = workspace / "meta-avocado"
    root = tmp_path / "wts"
    first = _head(repo)
    assert _run(workspace, root, first).returncode == 0
    second = _commit(repo, "b.txt", "b\n")
    res = _run(workspace, root, second)
    assert res.returncode == 0, res.stderr
    assert _head(root / "wt1") == second
    assert _marker(root / "wt1")["commit"] == second
    assert not (workspace / BUILD_DIR).exists()


def test_directory_without_marker_is_refused_untouched(workspace: Path, tmp_path: Path):
    root = tmp_path / "wts"
    dest = root / "wt1"
    dest.mkdir(parents=True)
    (dest / "keep.txt").write_text("mine\n")
    res = _run(workspace, root, _head(workspace / "meta-avocado"))
    assert res.returncode == EXIT_NO_MARKER
    assert sorted(p.name for p in dest.iterdir()) == ["keep.txt"]
    assert (dest / "keep.txt").read_text() == "mine\n"


def test_unowned_git_checkout_without_marker_is_refused(workspace: Path, tmp_path: Path):
    repo = workspace / "meta-avocado"
    root = tmp_path / "wts"
    first = _head(repo)
    _git("-C", repo, "worktree", "add", "-q", "--detach", root / "wt1", first)
    second = _commit(repo, "b.txt", "b\n")
    res = _run(workspace, root, second)
    assert res.returncode == EXIT_NO_MARKER
    assert MARKER_FILENAME in res.stderr
    assert _head(root / "wt1") == first


def test_marker_from_another_host_is_refused(workspace: Path, tmp_path: Path):
    repo = workspace / "meta-avocado"
    root = tmp_path / "wts"
    first = _head(repo)
    assert _run(workspace, root, first, host="other-host").returncode == 0
    second = _commit(repo, "b.txt", "b\n")
    res = _run(workspace, root, second)
    assert res.returncode == EXIT_FOREIGN_MARKER
    assert "other-host" in res.stderr
    assert HOST in res.stderr
    assert _head(root / "wt1") == first


def test_marker_from_another_path_is_refused(workspace: Path, tmp_path: Path):
    repo = workspace / "meta-avocado"
    root = tmp_path / "wts"
    first = _head(repo)
    assert _run(workspace, root, first, src="/elsewhere").returncode == 0
    second = _commit(repo, "b.txt", "b\n")
    res = _run(workspace, root, second)
    assert res.returncode == EXIT_FOREIGN_MARKER
    assert _head(root / "wt1") == first


def test_dirty_tree_is_refused(workspace: Path, tmp_path: Path):
    repo = workspace / "meta-avocado"
    root = tmp_path / "wts"
    first = _head(repo)
    assert _run(workspace, root, first).returncode == 0
    (root / "wt1" / "a.txt").write_text("edited\n")
    second = _commit(repo, "b.txt", "b\n")
    res = _run(workspace, root, second)
    assert res.returncode == EXIT_DIRTY
    assert "a.txt" in res.stderr
    assert _head(root / "wt1") == first
    assert (root / "wt1" / "a.txt").read_text() == "edited\n"


def test_held_bitbake_lock_is_refused(workspace: Path, tmp_path: Path):
    repo = workspace / "meta-avocado"
    root = tmp_path / "wts"
    first = _head(repo)
    assert _run(workspace, root, first).returncode == 0
    lock = workspace / BUILD_DIR / "build" / "bitbake.lock"
    lock.parent.mkdir(parents=True)
    second = _commit(repo, "b.txt", "b\n")
    with lock.open("w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        res = _run(workspace, root, second)
    assert res.returncode == EXIT_BUILD_RUNNING
    assert _head(root / "wt1") == first


def test_free_bitbake_lock_allows_move(workspace: Path, tmp_path: Path):
    repo = workspace / "meta-avocado"
    root = tmp_path / "wts"
    assert _run(workspace, root, _head(repo)).returncode == 0
    lock = workspace / BUILD_DIR / "build" / "bitbake.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("")
    second = _commit(repo, "b.txt", "b\n")
    res = _run(workspace, root, second)
    assert res.returncode == 0, res.stderr
    assert _head(root / "wt1") == second


def test_held_delivery_lock_is_refused(workspace: Path, tmp_path: Path):
    root = tmp_path / "wts"
    root.mkdir()
    with (root / ".wt1.lock").open("w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        res = _run(workspace, root, _head(workspace / "meta-avocado"))
    assert res.returncode == EXIT_LOCKED
    assert not (root / "wt1").exists()


def test_workspace_path_with_space_works(workspace: Path, tmp_path: Path):
    assert " " in str(workspace)
    root = tmp_path / "work trees"
    commit = _head(workspace / "meta-avocado")
    res = _run(workspace, root, commit)
    assert res.returncode == 0, res.stderr
    assert _head(root / "wt1") == commit


@pytest.mark.parametrize("name", ["", "a/b", "x y", "$(id)", "../up"])
def test_invalid_name_is_rejected(name: str):
    with pytest.raises(ReleaseRoutingError):
        ensure_worktree_script(
            DeliveryTarget(workspace="/w", worktree_root="/r"),
            MarkerFields(source_host=HOST, source_path=SRC, name=name, commit="abc", snapshot=False),
        )


_FORBIDDEN = (
    "worktree remove",
    "worktree prune",
    "--force",
    "rm ",
    " -f ",
    "reset --hard",
    "clean -",
    "update-ref -d",
    "branch -D",
    "checkout --",
    " gc",
    "rmdir",
    " mv ",
    "unlink",
    "--prune",
    "--del",
)


def test_script_has_no_destructive_operations():
    script = ensure_worktree_script(
        DeliveryTarget(workspace="/w", worktree_root="/r"),
        MarkerFields(source_host=HOST, source_path=SRC, name="wt1", commit="abc", snapshot=True),
        source_path_aliases=("/alias",),
    )
    for forbidden in _FORBIDDEN:
        assert forbidden not in script, forbidden


def test_build_guard_has_no_destructive_operations():
    guard = build_guard(worktree_root="/r", name="wt1", commit="abc")
    for forbidden in _FORBIDDEN:
        assert forbidden not in guard, forbidden


def test_build_lock_path_is_the_scripts_lock(workspace: Path, tmp_path: Path):
    repo = workspace / "meta-avocado"
    root = tmp_path / "wts"
    lock = build_lock_path(str(root), "wt1")
    assert f"build_lock_file={shlex.quote(lock)}\n" in _script(workspace, root, _head(repo))
    assert shlex.quote(lock) in build_guard(worktree_root=str(root), name="wt1", commit="abc")
    assert _run(workspace, root, _head(repo)).returncode == 0
    second = _commit(repo, "b.txt", "b\n")
    assert _run(workspace, root, second).returncode == 0
    # The move path is the only one that opens the lock, so it now exists.
    assert sorted(p.name for p in root.iterdir() if p.name.endswith(".build.lock")) == [Path(lock).name]


def test_marker_roundtrip():
    text = marker_json(
        MarkerFields(source_host=HOST, source_path=SRC, name="wt1", commit="abc", snapshot=True),
        delivered_at="2026-01-01T00:00:00+00:00",
    )
    assert parse_marker(text) == {
        "source_host": HOST,
        "source_path": SRC,
        "name": "wt1",
        "commit": "abc",
        "snapshot": True,
        "delivered_at": "2026-01-01T00:00:00+00:00",
    }


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "[]",
        json.dumps({"source_host": HOST}),
        json.dumps(
            {
                "source_host": HOST,
                "source_path": SRC,
                "name": "wt1",
                "commit": "abc",
                "snapshot": "yes",
                "delivered_at": "t",
            }
        ),
    ],
)
def test_parse_marker_rejects_malformed(text: str):
    with pytest.raises(ValueError):
        parse_marker(text)


def test_describe_exit_names_each_refusal():
    assert describe_exit(0) == "delivered"
    codes = (
        EXIT_LOCKED,
        EXIT_NO_MARKER,
        EXIT_FOREIGN_MARKER,
        EXIT_DIRTY,
        EXIT_BUILD_RUNNING,
        EXIT_MOVED,
        EXIT_NO_BUILD_LOCK,
    )
    assert len({describe_exit(code) for code in codes}) == len(codes)
    for code in codes:
        assert describe_exit(code) != describe_exit(99)
    assert "99" in describe_exit(99)


def test_same_commit_with_hand_edits_is_refused(workspace: Path, tmp_path: Path):
    repo = workspace / "meta-avocado"
    root = tmp_path / "wts"
    first = _head(repo)
    assert _run(workspace, root, first).returncode == 0
    (root / "wt1" / "a.txt").write_text("edited\n")
    res = _run(workspace, root, first)
    assert res.returncode == EXIT_DIRTY
    assert (root / "wt1" / "a.txt").read_text() == "edited\n"


def test_bitbake_lock_of_another_build_dir_is_refused(workspace: Path, tmp_path: Path):
    repo = workspace / "meta-avocado"
    root = tmp_path / "wts"
    first = _head(repo)
    assert _run(workspace, root, first).returncode == 0
    lock = workspace / "build-other-wt1" / "build" / "bitbake.lock"
    lock.parent.mkdir(parents=True)
    second = _commit(repo, "b.txt", "b\n")
    with lock.open("w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        res = _run(workspace, root, second)
    assert res.returncode == EXIT_BUILD_RUNNING
    assert _head(root / "wt1") == first


def _interrupt_after_add(root: Path, commit: str, *, host: str = HOST, head_commit: str | None = None) -> Path:
    """Leave the state a script killed between ``worktree add`` and the marker write leaves.

    The marker is removed and the pending record holds the text the script wrote
    before the add. ``head_commit`` is the commit the pending record names.
    """
    gitdir = Path(_git("-C", root / "wt1", "rev-parse", "--absolute-git-dir"))
    (gitdir / MARKER_FILENAME).unlink()
    (root / ".wt1.pending").write_text(
        marker_json(
            MarkerFields(source_host=host, source_path=SRC, name="wt1", commit=head_commit or commit, snapshot=False)
        )
    )
    return gitdir


def test_consumed_pending_record_is_empty_after_delivery(workspace: Path, tmp_path: Path):
    root = tmp_path / "wts"
    assert _run(workspace, root, _head(workspace / "meta-avocado")).returncode == 0
    assert (root / ".wt1.pending").stat().st_size == 0


def test_interrupted_delivery_is_completed_by_the_same_owner(workspace: Path, tmp_path: Path):
    root = tmp_path / "wts"
    first = _head(workspace / "meta-avocado")
    assert _run(workspace, root, first).returncode == 0
    gitdir = _interrupt_after_add(root, first)
    res = _run(workspace, root, first)
    assert res.returncode == 0, res.stderr
    assert json.loads((gitdir / MARKER_FILENAME).read_text())["source_host"] == HOST
    assert (root / ".wt1.pending").stat().st_size == 0


def test_interrupted_delivery_is_not_completed_for_another_owner(workspace: Path, tmp_path: Path):
    root = tmp_path / "wts"
    first = _head(workspace / "meta-avocado")
    assert _run(workspace, root, first).returncode == 0
    gitdir = _interrupt_after_add(root, first, host="other")
    res = _run(workspace, root, first)
    assert res.returncode != 0
    assert not (gitdir / MARKER_FILENAME).exists()


def test_consumed_pending_record_does_not_vouch_for_a_worktree_without_a_marker(workspace: Path, tmp_path: Path):
    root = tmp_path / "wts"
    first = _head(workspace / "meta-avocado")
    assert _run(workspace, root, first).returncode == 0
    gitdir = Path(_git("-C", root / "wt1", "rev-parse", "--absolute-git-dir"))
    (gitdir / MARKER_FILENAME).unlink()
    res = _run(workspace, root, first)
    assert res.returncode == EXIT_NO_MARKER
    assert not (gitdir / MARKER_FILENAME).exists()


def test_pending_record_for_another_commit_does_not_vouch_for_the_worktree(workspace: Path, tmp_path: Path):
    repo = workspace / "meta-avocado"
    root = tmp_path / "wts"
    first = _head(repo)
    assert _run(workspace, root, first).returncode == 0
    other = _commit(repo, "b.txt", "b\n")
    gitdir = _interrupt_after_add(root, first, head_commit=other)
    res = _run(workspace, root, first)
    assert res.returncode == EXIT_NO_MARKER
    assert not (gitdir / MARKER_FILENAME).exists()


def test_shared_build_lock_blocks_a_move(workspace: Path, tmp_path: Path):
    repo = workspace / "meta-avocado"
    root = tmp_path / "wts"
    first = _head(repo)
    assert _run(workspace, root, first).returncode == 0
    second = _commit(repo, "b.txt", "b\n")
    with (root / ".wt1.build.lock").open("w") as fh:
        fcntl.flock(fh, fcntl.LOCK_SH | fcntl.LOCK_NB)
        res = _run(workspace, root, second)
    assert res.returncode == EXIT_BUILD_RUNNING
    assert _head(root / "wt1") == first


def test_build_guard_runs_only_at_the_delivered_commit(workspace: Path, tmp_path: Path):
    repo = workspace / "meta-avocado"
    root = tmp_path / "wts"
    first = _head(repo)
    assert _run(workspace, root, first).returncode == 0
    ok = subprocess.run(
        ["bash", "-c", build_guard(worktree_root=str(root), name="wt1", commit=first) + " true"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert ok.returncode == 0, ok.stderr
    stale = subprocess.run(
        ["bash", "-c", build_guard(worktree_root=str(root), name="wt1", commit="0" * 40) + " true"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert stale.returncode == EXIT_MOVED
    assert "moved by another dispatch" in stale.stderr


def _wait_for(predicate, what: str, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            pytest.fail(f"timed out waiting for {what}")
        time.sleep(0.05)


def _flock_probe(lock: str, mode: str) -> int:
    return subprocess.run(["flock", "-n", mode, lock, "true"], timeout=10, check=False).returncode


def _slow_checkout_env(tmp_path: Path) -> tuple[dict[str, str], Path]:
    """Return an env whose ``git checkout`` touches a mark file, then sleeps."""
    shim = tmp_path / "shim"
    shim.mkdir()
    mark = tmp_path / "in-checkout"
    real_git = shutil.which("git")
    assert real_git is not None
    (shim / "git").write_text(
        "#!/bin/bash\n"
        'for a in "$@"; do\n'
        f'    if [ "$a" = checkout ]; then : > {shlex.quote(str(mark))}; sleep 1.5; fi\n'
        "done\n"
        f'exec {shlex.quote(real_git)} "$@"\n'
    )
    (shim / "git").chmod(0o755)
    return {**os.environ, **_GIT_ENV, "PATH": f"{shim}:{os.environ['PATH']}"}, mark


def _start_slow_move(workspace: Path, root: Path, tmp_path: Path) -> tuple[subprocess.Popen, str, str]:
    """Deliver HEAD, then start moving to a new commit with a checkout that lingers."""
    repo = workspace / "meta-avocado"
    first = _head(repo)
    assert _run(workspace, root, first).returncode == 0
    second = _commit(repo, "b.txt", "b\n")
    env, mark = _slow_checkout_env(tmp_path)
    proc = subprocess.Popen(
        ["bash", "-c", _script(workspace, root, second)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    _wait_for(mark.exists, "the move to reach git checkout")
    return proc, first, second


def test_move_holds_the_build_lock_through_the_checkout(workspace: Path, tmp_path: Path):
    root = tmp_path / "wts"
    proc, _, second = _start_slow_move(workspace, root, tmp_path)
    lock = build_lock_path(str(root), "wt1")
    held = _flock_probe(lock, "-s")
    _, err = proc.communicate(timeout=30)
    assert proc.returncode == 0, err
    assert held != 0
    assert _flock_probe(lock, "-s") == 0
    assert _head(root / "wt1") == second


def test_build_started_during_a_move_refuses_the_moved_tree(workspace: Path, tmp_path: Path):
    root = tmp_path / "wts"
    proc, first, second = _start_slow_move(workspace, root, tmp_path)
    guarded = subprocess.run(
        ["bash", "-c", build_guard(worktree_root=str(root), name="wt1", commit=first) + " true"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    _, err = proc.communicate(timeout=30)
    assert proc.returncode == 0, err
    assert guarded.returncode == EXIT_MOVED, guarded.stderr
    assert _head(root / "wt1") == second


def test_build_guard_holds_the_lock_while_the_build_runs(workspace: Path, tmp_path: Path):
    root = tmp_path / "wts"
    commit = _head(workspace / "meta-avocado")
    assert _run(workspace, root, commit).returncode == 0
    ready = tmp_path / "ready"
    build = f"bash -c {shlex.quote(f': > {shlex.quote(str(ready))}; sleep 30')}"
    proc = subprocess.Popen(
        ["bash", "-c", f"{build_guard(worktree_root=str(root), name='wt1', commit=commit)} {build}"],
        start_new_session=True,
    )
    lock = build_lock_path(str(root), "wt1")
    try:
        _wait_for(ready.exists, "the guarded build to start")
        assert _flock_probe(lock, "-x") != 0
    finally:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=10)
    assert _flock_probe(lock, "-x") == 0


def _user_systemd_available() -> bool:
    if shutil.which("systemd-run") is None or shutil.which("systemctl") is None:
        return False
    probe = subprocess.run(["systemctl", "--user", "show-environment"], capture_output=True, timeout=10, check=False)
    return probe.returncode == 0


@pytest.mark.skipif(not _user_systemd_available(), reason="needs a systemd user manager")
def test_sigint_to_the_unit_lets_a_guarded_build_drain(workspace: Path, tmp_path: Path):
    root = tmp_path / "wts"
    commit = _head(workspace / "meta-avocado")
    assert _run(workspace, root, commit).returncode == 0
    fake = tmp_path / "fakebuild"
    fake.write_text(
        "#!/bin/bash\n"
        "trap 'echo draining; sleep 1; echo drained; exit 0' INT\n"
        "echo started\n"
        "for i in $(seq 60); do sleep 1; done\n"
    )
    fake.chmod(0o755)
    log = tmp_path / "log"
    rc_file = tmp_path / "log.rc"
    q = shlex.quote
    guard = build_guard(worktree_root=str(root), name="wt1", commit=commit)
    # The rc sentinel is what the dispatch follower waits for, so it must be
    # written after the build has drained.
    wrapped = (
        f"{guard} {q(str(fake))} > {q(str(log))} 2>&1; rc=$?; printf '%s\\n' \"$rc\" > {q(str(rc_file))}; exit $rc"
    )
    unit = f"bakar-test-guard-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["systemd-run", "--user", "--collect", "--quiet", f"--unit={unit}", "--", "bash", "-c", wrapped],
        check=True,
        timeout=30,
    )
    try:
        _wait_for(lambda: log.exists() and "started" in log.read_text(), "the fake build to start")
        subprocess.run(["systemctl", "--user", "kill", "--signal=SIGINT", unit], check=True, timeout=10)
        _wait_for(lambda: rc_file.exists() and rc_file.read_text().strip() != "", "the rc sentinel", timeout=20)
    finally:
        subprocess.run(["systemctl", "--user", "stop", unit], capture_output=True, timeout=30, check=False)
    assert rc_file.read_text().strip() == "0"
    assert "drained" in log.read_text()


def test_failed_add_empties_the_pending_record(workspace: Path, tmp_path: Path):
    repo = workspace / "meta-avocado"
    root = tmp_path / "wts"
    dest = root / "wt1"
    first = _head(repo)
    assert _run(workspace, root, first).returncode == 0
    # git still has the moved-aside worktree registered, so the add fails.
    dest.rename(dest.with_name("wt1.moved-aside"))
    second = _commit(repo, "b.txt", "b\n")
    res = _run(workspace, root, second)
    assert res.returncode != 0
    assert (root / ".wt1.pending").stat().st_size == 0
    _git("-C", repo, "worktree", "prune")
    _git("-C", repo, "worktree", "add", "-q", "--detach", dest, second)
    third = _commit(repo, "c.txt", "c\n")
    res = _run(workspace, root, third)
    assert res.returncode == EXIT_NO_MARKER
    assert _head(dest) == second


@pytest.mark.parametrize("pending", ["", "record"])
def test_unborn_head_without_marker_is_refused(workspace: Path, tmp_path: Path, pending: str):
    root = tmp_path / "wts"
    dest = root / "wt1"
    _git("init", "-q", dest)
    commit = _head(workspace / "meta-avocado")
    if pending:
        (root / ".wt1.pending").write_text(
            marker_json(MarkerFields(source_host=HOST, source_path=SRC, name="wt1", commit=commit, snapshot=False))
        )
    res = _run(workspace, root, commit)
    assert res.returncode == EXIT_NO_MARKER, res.stderr
    assert "fatal" not in res.stderr


CANON = "/home/me/real/meta-avocado"


def test_marker_with_the_canonical_spelling_is_accepted(workspace: Path, tmp_path: Path):
    repo = workspace / "meta-avocado"
    root = tmp_path / "wts"
    assert _exec(_script(workspace, root, _head(repo), src=CANON, aliases=(SRC,))).returncode == 0
    assert _marker(root / "wt1")["source_path"] == CANON
    second = _commit(repo, "b.txt", "b\n")
    res = _exec(_script(workspace, root, second, src=CANON, aliases=(SRC,)))
    assert res.returncode == 0, res.stderr
    assert _head(root / "wt1") == second


def test_marker_with_an_alias_spelling_is_accepted(workspace: Path, tmp_path: Path):
    repo = workspace / "meta-avocado"
    root = tmp_path / "wts"
    # A marker written before the canonical spelling was used: the logical path.
    assert _run(workspace, root, _head(repo), src=SRC).returncode == 0
    second = _commit(repo, "b.txt", "b\n")
    res = _exec(_script(workspace, root, second, src=CANON, aliases=(SRC,)))
    assert res.returncode == 0, res.stderr
    assert _head(root / "wt1") == second
    assert _marker(root / "wt1")["source_path"] == CANON


def test_marker_from_a_third_path_is_refused_despite_aliases(workspace: Path, tmp_path: Path):
    repo = workspace / "meta-avocado"
    root = tmp_path / "wts"
    first = _head(repo)
    assert _run(workspace, root, first, src="/elsewhere").returncode == 0
    second = _commit(repo, "b.txt", "b\n")
    res = _exec(_script(workspace, root, second, src=CANON, aliases=(SRC,)))
    assert res.returncode == EXIT_FOREIGN_MARKER
    assert "/elsewhere" in res.stderr
    assert _head(root / "wt1") == first


def test_old_style_pending_record_is_accepted_via_alias(workspace: Path, tmp_path: Path):
    root = tmp_path / "wts"
    first = _head(workspace / "meta-avocado")
    assert _run(workspace, root, first).returncode == 0
    gitdir = _interrupt_after_add(root, first)
    res = _exec(_script(workspace, root, first, src=CANON, aliases=(SRC,)))
    assert res.returncode == 0, res.stderr
    assert (gitdir / MARKER_FILENAME).exists()
    assert (root / ".wt1.pending").stat().st_size == 0


def _run_guard(root: Path, commit: str, ran: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    build = f"touch {shlex.quote(str(ran))}"
    return subprocess.run(
        ["bash", "-c", f"{build_guard(worktree_root=str(root), name='wt1', commit=commit)} {build}"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


@pytest.mark.skipif(os.geteuid() == 0, reason="root opens files in a read-only directory")
def test_build_guard_refuses_to_run_when_the_lock_cannot_be_opened(workspace: Path, tmp_path: Path):
    root = tmp_path / "wts"
    commit = _head(workspace / "meta-avocado")
    assert _run(workspace, root, commit).returncode == 0
    # No move has happened, so the build lock does not exist and cannot be created.
    assert not Path(build_lock_path(str(root), "wt1")).exists()
    ran = tmp_path / "build-ran"
    root.chmod(0o555)
    try:
        res = _run_guard(root, commit, ran)
    finally:
        root.chmod(0o755)
    assert res.returncode == EXIT_NO_BUILD_LOCK, res.stderr
    assert "cannot open the build lock" in res.stderr
    assert not ran.exists()


def test_build_guard_refuses_to_run_when_the_lock_directory_is_missing(tmp_path: Path):
    ran = tmp_path / "build-ran"
    res = _run_guard(tmp_path / "missing" / "wts", "0" * 40, ran)
    assert res.returncode == EXIT_NO_BUILD_LOCK, res.stderr
    assert "cannot open the build lock" in res.stderr
    assert not ran.exists()


def test_build_guard_refuses_to_run_when_flock_fails(workspace: Path, tmp_path: Path):
    root = tmp_path / "wts"
    commit = _head(workspace / "meta-avocado")
    assert _run(workspace, root, commit).returncode == 0
    # Stands in for ENOLCK on NFS: the open succeeds and the lock call fails.
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "flock").write_text("#!/bin/sh\necho 'flock: No locks available' >&2\nexit 1\n")
    (shim / "flock").chmod(0o755)
    env = {**os.environ, **_GIT_ENV, "PATH": f"{shim}:{os.environ['PATH']}"}
    ran = tmp_path / "build-ran"
    res = _run_guard(root, commit, ran, env=env)
    assert res.returncode == EXIT_NO_BUILD_LOCK, res.stderr
    assert "cannot take the build lock" in res.stderr
    assert not ran.exists()


def _install_lingering_hook(workspace: Path, pids: Path) -> None:
    """Make every checkout start a background child that outlives git, as some hooks do."""
    hook = workspace / "meta-avocado" / ".git" / "hooks" / "post-checkout"
    hook.parent.mkdir(parents=True, exist_ok=True)
    # The child's stdio goes to /dev/null so only the lock descriptors, never
    # the captured pipes, can tie it to the delivery script.
    hook.write_text(f"#!/bin/sh\n(sleep 20) </dev/null >/dev/null 2>&1 &\necho $! >> {shlex.quote(str(pids))}\n")
    hook.chmod(0o755)


def _kill_pids(pids: Path) -> None:
    if not pids.exists():
        return
    for line in pids.read_text().split():
        try:
            os.kill(int(line), signal.SIGKILL)
        except ProcessLookupError:
            continue


def test_hook_children_do_not_inherit_the_locks(workspace: Path, tmp_path: Path):
    repo = workspace / "meta-avocado"
    root = tmp_path / "wts"
    pids = tmp_path / "hook-pids"
    _install_lingering_hook(workspace, pids)
    dispatch_lock = str(root / ".wt1.lock")
    build_lock = build_lock_path(str(root), "wt1")
    try:
        first = _head(repo)
        assert _run(workspace, root, first).returncode == 0
        assert pids.exists(), "the post-checkout hook did not run on worktree add"
        assert _flock_probe(dispatch_lock, "-x") == 0
        second = _commit(repo, "b.txt", "b\n")
        res = _run(workspace, root, second)
        assert res.returncode == 0, res.stderr
        assert len(pids.read_text().split()) == 2, "the post-checkout hook did not run on the move"
        assert _flock_probe(dispatch_lock, "-x") == 0
        assert _flock_probe(build_lock, "-x") == 0
        # The redelivery and the build the reviewer saw refused and hung.
        again = _run(workspace, root, second)
        assert again.returncode == 0, again.stderr
        ran = tmp_path / "build-ran"
        assert _run_guard(root, second, ran).returncode == 0
        assert ran.exists()
    finally:
        _kill_pids(pids)
