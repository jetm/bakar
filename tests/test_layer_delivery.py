"""Tests for bakar.steps.layer_delivery against real temporary git repositories."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

from bakar.bsp_detect import LinkedWorktree
from bakar.release_workspace import ReleaseRoutingError
from bakar.steps.layer_delivery import DeliveryError, delivered_name, push_argv, snapshot_commit

if TYPE_CHECKING:
    from pathlib import Path


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _identity(path: Path) -> None:
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "t")
    _git(path, "config", "commit.gpgsign", "false")


def _make_repo(path: Path, commits: int = 2) -> str:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    _identity(path)
    (path / ".gitignore").write_text("ignored.txt\n")
    _git(path, "add", ".gitignore")
    for i in range(commits):
        (path / "f.txt").write_text(f"{i}\n")
        _git(path, "add", "f.txt")
        _git(path, "commit", "-q", "-m", f"c{i}")
    return _git(path, "rev-parse", "HEAD")


def _dirty(repo: Path) -> None:
    (repo / "f.txt").write_text("edited\n")
    (repo / "new.txt").write_text("untracked\n")
    (repo / "ignored.txt").write_text("ignored\n")


def test_clean_returns_head(tmp_path: Path) -> None:
    repo = tmp_path / "meta-avocado"
    head = _make_repo(repo)
    assert snapshot_commit(repo, cap_mb=1) == (head, False)


def test_dirty_snapshot_holds_changes_and_leaves_repo_untouched(tmp_path: Path) -> None:
    repo = tmp_path / "meta-avocado"
    head = _make_repo(repo)
    _dirty(repo)
    index_before = (repo / ".git" / "index").read_bytes()
    status_before = _git(repo, "status", "--porcelain", "--untracked-files=all")

    sha, is_snapshot = snapshot_commit(repo, cap_mb=1)

    assert is_snapshot
    assert sha != head
    assert _git(repo, "rev-parse", f"{sha}^") == head
    assert _git(repo, "show", f"{sha}:f.txt") == "edited"
    assert _git(repo, "show", f"{sha}:new.txt") == "untracked"
    assert "ignored.txt" not in _git(repo, "ls-tree", "--name-only", sha).split()
    assert _git(repo, "log", "-1", "--format=%s", sha) == f"bakar dispatch snapshot of main@{head[:12]}"
    assert _git(repo, "rev-parse", "main") == head
    assert (repo / ".git" / "index").read_bytes() == index_before
    assert _git(repo, "status", "--porcelain", "--untracked-files=all") == status_before
    assert _git(repo, "stash", "list") == ""


def test_same_dirty_tree_gives_same_snapshot(tmp_path: Path) -> None:
    repo = tmp_path / "meta-avocado"
    _make_repo(repo)
    _dirty(repo)
    first, _ = snapshot_commit(repo, cap_mb=1)
    second, _ = snapshot_commit(repo, cap_mb=1)
    assert first == second


def test_untracked_over_cap_is_refused(tmp_path: Path) -> None:
    repo = tmp_path / "meta-avocado"
    _make_repo(repo)
    (repo / "big.bin").write_bytes(b"\0" * (2 * 1024 * 1024))
    with pytest.raises(DeliveryError, match=r"2\.0 MiB.*1 MiB"):
        snapshot_commit(repo, cap_mb=1)


def test_delivered_name_prefers_worktree_dir(tmp_path: Path) -> None:
    repo = tmp_path / "meta-avocado"
    _make_repo(repo)
    wt = tmp_path / "wt" / "my feature"
    assert delivered_name(repo, LinkedWorktree(checkout=wt, main_checkout=repo)) == "my-feature"


def test_delivered_name_falls_back_to_branch(tmp_path: Path) -> None:
    repo = tmp_path / "meta-avocado"
    _make_repo(repo)
    _git(repo, "checkout", "-q", "-b", "user/fix")
    assert delivered_name(repo, None) == "user-fix"


def test_delivered_name_unusable_raises(tmp_path: Path) -> None:
    repo = tmp_path / "meta-avocado"
    _make_repo(repo)
    with pytest.raises(ReleaseRoutingError):
        delivered_name(repo, LinkedWorktree(checkout=tmp_path / "...", main_checkout=repo))


def test_push_argv_shape_without_force(tmp_path: Path) -> None:
    commit = "a" * 40
    argv = push_argv(tmp_path, "pc2", "/ws/meta-avocado", commit, "wt1")
    assert argv == [
        "git",
        "-C",
        str(tmp_path),
        "push",
        "--receive-pack=git -c receive.autogc=false -c receive.shallowUpdate=true receive-pack",
        "ssh://pc2/ws/meta-avocado",
        f"{commit}:refs/bakar/wt1/{commit[:12]}",
    ]
    assert "--force" not in argv
    assert "-f" not in argv
    assert not any(a.startswith("+") for a in argv)
    assert not any(a.startswith("--force") for a in argv)


@pytest.mark.parametrize("name", ["", "a b", "../x", "x;rm", "+x"])
def test_push_argv_refuses_invalid_name(tmp_path: Path, name: str) -> None:
    with pytest.raises(DeliveryError):
        push_argv(tmp_path, "pc2", "/ws/meta-avocado", "a" * 40, name)


@pytest.mark.parametrize("commit", ["+" + "a" * 40, "HEAD", "a" * 12])
def test_push_argv_refuses_non_commit(tmp_path: Path, commit: str) -> None:
    with pytest.raises(DeliveryError):
        push_argv(tmp_path, "pc2", "/ws/meta-avocado", commit, "wt1")


def test_shallow_snapshot_pushes_into_shallow_clone_and_checks_out(tmp_path: Path) -> None:
    origin = tmp_path / "origin"
    _make_repo(origin, commits=4)
    local = tmp_path / "local"
    remote = tmp_path / "remote"
    for clone in (local, remote):
        subprocess.run(
            ["git", "clone", "-q", "--depth", "1", f"file://{origin}", str(clone)],
            check=True,
            capture_output=True,
        )
        _identity(clone)
    assert _git(local, "rev-parse", "--is-shallow-repository") == "true"
    assert _git(remote, "rev-parse", "--is-shallow-repository") == "true"
    _dirty(local)
    sha, is_snapshot = snapshot_commit(local, cap_mb=1)
    assert is_snapshot

    argv = push_argv(local, "host", str(remote), sha, "wt1")
    url_index = argv.index(f"ssh://host{remote}")
    argv[url_index] = str(remote)
    subprocess.run(argv, check=True, capture_output=True)

    ref = f"refs/bakar/wt1/{sha[:12]}"
    assert _git(remote, "rev-parse", ref) == sha
    wt = tmp_path / "remote-wt"
    _git(remote, "worktree", "add", "--detach", str(wt), sha)
    assert _git(wt, "rev-parse", "HEAD") == sha
    assert (wt / "new.txt").read_text() == "untracked\n"
