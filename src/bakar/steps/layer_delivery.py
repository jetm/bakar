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

import fnmatch
import os
import posixpath
import re
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from bakar.release_workspace import is_valid_delivered_name, sanitize_delivered_name

if TYPE_CHECKING:
    from bakar.bsp_detect import LinkedWorktree

_RECEIVE_PACK = "git -c receive.autogc=false receive-pack"
_COMMIT_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
# Basenames of key material a snapshot refuses to carry, compared lowercased so
# that a file named ID_RSA or SERVER.PEM is not let through by its case.
_KEY_PATTERNS = (
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.kdbx",
    "*.gpg",
    "*.jks",
    "*.keystore",
    "*.age",
    "*.ppk",
    "*.p8",
    "id_rsa*",
    "id_dsa*",
    "id_ed25519*",
    "id_ecdsa*",
    ".env",
    ".env.*",
    ".netrc",
    ".git-credentials",
    ".pgpass",
    "credentials",
)
# Basenames a key pattern above would catch that carry no secret: public halves
# of ssh keys and the example files a project commits to document its .env.
_KEY_EXEMPT_PATTERNS = (
    "id_*.pub",
    ".env.example",
    ".env.sample",
    ".env.template",
)


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


def snapshot_untracked_paths(checkout: Path) -> list[str]:
    """Return the untracked, non-ignored paths a snapshot of ``checkout`` would carry."""
    out = _git(checkout, "ls-files", "--others", "--exclude-standard", "-z", text=False).stdout
    return sorted(os.fsdecode(raw) for raw in out.split(b"\0") if raw)


def _untracked_bytes(checkout: Path, paths: list[str]) -> int:
    total = 0
    for path in paths:
        try:
            total += (checkout / path).lstat().st_size
        except OSError:
            continue
    return total


def _looks_like_key(path: str) -> bool:
    base = posixpath.basename(path).lower()
    if any(fnmatch.fnmatchcase(base, pat) for pat in _KEY_EXEMPT_PATTERNS):
        return False
    return any(fnmatch.fnmatchcase(base, pat) for pat in _KEY_PATTERNS)


def _key_material(paths: list[str]) -> list[str]:
    return [p for p in paths if _looks_like_key(p)]


def snapshot_commit(checkout: Path, *, cap_mb: int) -> tuple[str, bool]:
    """Return ``(commit, is_snapshot)`` for delivering ``checkout``.

    A clean checkout yields HEAD. A dirty one yields a snapshot commit holding
    every tracked change and every untracked non-ignored file, refused when an
    untracked file looks like key material or the untracked files together
    exceed ``cap_mb`` MiB.
    """
    head = _git(checkout, "rev-parse", "HEAD").stdout.strip()
    status = _git(checkout, "status", "--porcelain", "--untracked-files=all").stdout
    if not status.strip():
        return head, False

    untracked_paths = snapshot_untracked_paths(checkout)
    # The snapshot lands in a permanent ref on a shared build host, so key
    # material must never ride along just because nobody ignored it.
    keys = _key_material(untracked_paths)
    if keys:
        raise DeliveryError(
            f"untracked files in {checkout} look like key material: {', '.join(keys)}; "
            "move them out of the checkout, ignore them, or commit them after review"
        )
    untracked = _untracked_bytes(checkout, untracked_paths)
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


def is_shallow(checkout: Path) -> bool:
    """Whether ``checkout`` is a shallow clone."""
    result = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "--is-shallow-repository"],
        check=False,
        capture_output=True,
        text=True,
    )
    return (result.stdout or "").strip() == "true"


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
