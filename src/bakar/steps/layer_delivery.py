"""Deliver a local meta-avocado checkout to a remote workspace (local half).

A dirty checkout is captured as a snapshot commit whose parent is HEAD. The
snapshot is built through a temporary index file, so the user's real index,
branch and working tree are never touched and ``git stash`` is never used.
Author and committer dates are pinned to HEAD's committer date, which makes the
snapshot of an unchanged dirty tree the same commit on every dispatch.

The commit is then pushed, without any force, to
``refs/bakar/<name>/<commit12>`` of the remote clone; the remote half
(:mod:`bakar.steps.layer_delivery_remote`) turns that into a worktree.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from bakar.release_workspace import is_valid_delivered_name, sanitize_delivered_name

if TYPE_CHECKING:
    from bakar.bsp_detect import LinkedWorktree

_RECEIVE_PACK = "git -c receive.autogc=false -c receive.shallowUpdate=true receive-pack"
_COMMIT_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")


class DeliveryError(Exception):
    """A checkout cannot be delivered to the remote workspace."""


def _git(
    checkout: Path, *args: str, env: dict[str, str] | None = None, text: bool = True
) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", "--no-optional-locks", "-C", str(checkout), *args],
        capture_output=True,
        text=text,
        env=env,
        check=False,
    )
    if proc.returncode != 0:
        stderr = proc.stderr if text else proc.stderr.decode(errors="replace")
        raise DeliveryError(f"git {' '.join(args)} failed in {checkout}: {stderr.strip()}")
    return proc


def _untracked_bytes(checkout: Path) -> int:
    out = _git(checkout, "ls-files", "--others", "--exclude-standard", "-z", text=False).stdout
    total = 0
    for raw in out.split(b"\0"):
        if not raw:
            continue
        try:
            total += (checkout / os.fsdecode(raw)).lstat().st_size
        except OSError:
            continue
    return total


def snapshot_commit(checkout: Path, *, cap_mb: int) -> tuple[str, bool]:
    """Return ``(commit, is_snapshot)`` for delivering ``checkout``.

    A clean checkout yields HEAD. A dirty one yields a snapshot commit holding
    every tracked change and every untracked non-ignored file, refused when the
    untracked files together exceed ``cap_mb`` MiB.
    """
    head = _git(checkout, "rev-parse", "HEAD").stdout.strip()
    status = _git(checkout, "status", "--porcelain", "--untracked-files=all").stdout
    if not status.strip():
        return head, False

    untracked = _untracked_bytes(checkout)
    cap = cap_mb * 1024 * 1024
    if untracked > cap:
        raise DeliveryError(
            f"untracked files in {checkout} total {untracked / (1024 * 1024):.1f} MiB, "
            f"above the {cap_mb} MiB snapshot cap; commit, ignore or remove them, or raise the cap"
        )

    branch = _git(checkout, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    date = _git(checkout, "log", "-1", "--format=%ct %z", "HEAD").stdout.strip()
    with tempfile.TemporaryDirectory(prefix="bakar-snapshot-") as tmp:
        env = {
            **os.environ,
            "GIT_INDEX_FILE": str(Path(tmp) / "index"),
            "GIT_AUTHOR_DATE": date,
            "GIT_COMMITTER_DATE": date,
        }
        _git(checkout, "read-tree", "HEAD", env=env)
        _git(checkout, "add", "-A", env=env)
        tree = _git(checkout, "write-tree", env=env).stdout.strip()
        sha = _git(
            checkout,
            "commit-tree",
            "--no-gpg-sign",
            tree,
            "-p",
            head,
            "-m",
            f"bakar dispatch snapshot of {branch}@{head[:12]}",
            env=env,
        ).stdout.strip()
    return sha, True


def delivered_name(checkout: Path, worktree: LinkedWorktree | None) -> str:
    """Name a delivery after the worktree directory, else the current branch."""
    if worktree is not None:
        return sanitize_delivered_name(worktree.checkout.name)
    branch = _git(checkout, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if branch == "HEAD":
        # Every detached checkout would share one remote worktree named HEAD.
        raise DeliveryError(f"{checkout} has a detached HEAD; check out a branch or build from a linked worktree")
    return sanitize_delivered_name(branch)


def push_argv(checkout: Path, host: str, remote_repo: str, commit: str, name: str) -> list[str]:
    """Build the non-forced push of ``commit`` to ``refs/bakar/<name>/<commit12>``."""
    if not is_valid_delivered_name(name):
        raise DeliveryError(f"refusing to push under the unsafe name {name!r}")
    if _COMMIT_RE.fullmatch(commit) is None:
        raise DeliveryError(f"refusing to push {commit!r}: not a full commit id")
    if not remote_repo.startswith("/"):
        raise DeliveryError(f"remote repository path must be absolute, got {remote_repo!r}")
    return [
        "git",
        "-C",
        str(checkout),
        "push",
        f"--receive-pack={_RECEIVE_PACK}",
        f"ssh://{host}{remote_repo}",
        f"{commit}:refs/bakar/{name}/{commit[:12]}",
    ]
