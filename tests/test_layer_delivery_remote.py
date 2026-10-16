"""Tests for the remote worktree delivery script, run with bash on real repos."""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
from pathlib import Path

import pytest

from bakar.release_workspace import ReleaseRoutingError
from bakar.steps.layer_delivery_remote import (
    EXIT_BUILD_RUNNING,
    EXIT_DIRTY,
    EXIT_FOREIGN_MARKER,
    EXIT_LOCKED,
    EXIT_NO_MARKER,
    MARKER_FILENAME,
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


def _run(workspace: Path, root: Path, commit: str, *, host: str = HOST, src: str = SRC):
    script = ensure_worktree_script(
        workspace=str(workspace),
        worktree_root=str(root),
        name="wt1",
        commit=commit,
        snapshot=False,
        source_host=host,
        source_path=src,
        build_dir=BUILD_DIR,
    )
    env = {**os.environ, **_GIT_ENV}
    return subprocess.run(
        ["bash", "-c", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


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
            workspace="/w",
            worktree_root="/r",
            name=name,
            commit="abc",
            snapshot=False,
            source_host=HOST,
            source_path=SRC,
            build_dir=BUILD_DIR,
        )


def test_script_has_no_destructive_operations():
    script = ensure_worktree_script(
        workspace="/w",
        worktree_root="/r",
        name="wt1",
        commit="abc",
        snapshot=True,
        source_host=HOST,
        source_path=SRC,
        build_dir=BUILD_DIR,
    )
    for forbidden in ("worktree remove", "worktree prune", "--force", "rm ", " -f "):
        assert forbidden not in script


def test_marker_roundtrip():
    text = marker_json(
        source_host=HOST,
        source_path=SRC,
        name="wt1",
        commit="abc",
        snapshot=True,
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
    for code in (EXIT_LOCKED, EXIT_NO_MARKER, EXIT_FOREIGN_MARKER, EXIT_DIRTY, EXIT_BUILD_RUNNING):
        assert describe_exit(code) != describe_exit(99)
    assert "99" in describe_exit(99)
