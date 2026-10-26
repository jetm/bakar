"""Classify a shared workspace's pinned repositories for reuse by kas.

A release workspace is shared by every build that routes into it, so kas must
never move one of its clones. Each pinned repository is classified as
``frozen`` (already at its pin and clean: kas is told to leave it alone with
``url: null``), ``missing`` (kas clones it at its pin as usual) or ``side``
(on another commit or carrying local changes: kas is pointed at a separate
checkout under ``<workspace>/.bakar/pins/<name>/<commit12>`` instead).

Classification only ever runs ``git rev-parse HEAD`` and ``git status
--porcelain``, both with ``--no-optional-locks`` so not even the index stat
cache is refreshed.
"""

from __future__ import annotations

import fcntl
import os
import re
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import yaml

from bakar.gitutil import git_stdout

if TYPE_CHECKING:
    from collections.abc import Iterator

RepoStateKind = Literal["frozen", "missing", "side", "invalid"]

# The build puts <workspace>/bitbake/bin first on PATH and oe-core finds bitbake
# as ../bitbake, so a side checkout of either is never the copy the build runs.
NO_SIDE_CHECKOUT = ("bitbake", "openembedded-core")

_HEX_PIN = re.compile(r"[0-9a-fA-F]{4,40}")

_KAS_HEADER_VERSION = 16

# The holder runs a kas dump that may clone missing repositories, which on a
# slow link or a stalled fetch takes long; past this a waiter gives up loudly.
LOCK_WAIT_LIMIT_S = 20 * 60
_LOCK_POLL_S = 0.25
_LOCK_NOTICE_AFTER_S = 3.0

# git status on a large clone with a cold cache is legitimately slow, and a
# timeout there reads as "not clean", misclassifying a clean frozen repository.
_GIT_READ_TIMEOUT_S = 30 * 60


class RepoReuseError(Exception):
    """A shared repository cannot be reused without kas moving something."""


@dataclass(frozen=True)
class RepoState:
    name: str
    path: Path
    pin: str | None
    state: RepoStateKind
    side_path: Path | None = None
    side_exists: bool = False
    head: str | None = None
    clean: bool | None = None
    pin_present: bool | None = None
    blocking: str | None = None


@contextmanager
def workspace_lock(workspace: Path, *, wait_limit_s: float = LOCK_WAIT_LIMIT_S) -> Iterator[None]:
    """Hold the workspace's exclusive repository lock for the ``with`` body.

    Two builds that both classify a repository as ``missing`` would each let
    kas clone it, and the second kas would then check the first build's fresh
    clone out to its own pin. Serialising classify-through-dump closes that
    window: the second build classifies only after the first clone exists.

    The lock is polled rather than awaited so a waiter says why it is stuck
    and stops after ``wait_limit_s`` instead of hanging behind a holder whose
    clone is waiting on a credential prompt.

    Raises RuntimeError naming the lock file when the wait runs out.
    """
    lock_dir = workspace / ".bakar"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / "repos.lock"
    with lock_path.open("a") as fh:
        start = time.monotonic()
        noticed = False
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                waited = time.monotonic() - start
                if waited >= wait_limit_s:
                    raise RuntimeError(
                        f"gave up after waiting {waited:.0f}s for {lock_path}: another bakar build on this "
                        "workspace still holds it (its kas dump may be stuck cloning a repository)"
                    ) from None
                if not noticed and waited >= _LOCK_NOTICE_AFTER_S:
                    print(
                        f"waiting for {lock_path}: another bakar build on this workspace holds it",
                        file=sys.stderr,
                    )
                    noticed = True
                time.sleep(_LOCK_POLL_S)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


@dataclass(frozen=True)
class PinnedRepo:
    """One URL-bearing kas repository with its pin and checkout locations."""

    name: str
    url: str
    pin: str | None
    path: Path
    side_path: Path | None


@dataclass(frozen=True)
class RepoFacts:
    """What the local filesystem and git say about one ``PinnedRepo``."""

    exists: bool
    head: str | None
    clean: bool | None
    pin_present: bool | None
    side_exists: bool
    side_head: str | None
    side_clean: bool | None

    @classmethod
    def unknown(cls) -> RepoFacts:
        """Facts for a repository nothing could be observed about."""
        return cls(
            exists=False,
            head=None,
            clean=None,
            pin_present=None,
            side_exists=False,
            side_head=None,
            side_clean=None,
        )


def _resolve_path(name: str, entry: dict, workspace: Path) -> Path:
    raw = entry.get("path") or name
    path = Path(raw)
    return path if path.is_absolute() else workspace / path


def pinned_entries(repos: dict, workspace: Path) -> list[PinnedRepo]:
    """Walk a kas ``repos`` mapping in order, skipping url-less local entries."""
    entries: list[PinnedRepo] = []
    for name, raw_entry in (repos or {}).items():
        entry = raw_entry or {}
        url = entry.get("url")
        if not url:
            continue
        commit = entry.get("commit")
        pin = str(commit) if commit else None
        side_path = workspace / ".bakar" / "pins" / name / pin[:12] if pin else None
        entries.append(PinnedRepo(name, str(url), pin, _resolve_path(name, entry, workspace), side_path))
    return entries


def _head_and_clean(path: Path) -> tuple[str | None, bool | None]:
    head = git_stdout(path, "--no-optional-locks", "rev-parse", "HEAD", timeout=_GIT_READ_TIMEOUT_S)
    status = git_stdout(path, "--no-optional-locks", "status", "--porcelain", timeout=_GIT_READ_TIMEOUT_S)
    return (head.strip() if head is not None else None), (status.strip() == "" if status is not None else None)


def _gather_one(entry: PinnedRepo) -> RepoFacts:
    if not entry.path.is_dir():
        return RepoFacts.unknown()
    head, clean = _head_and_clean(entry.path)
    pin_present: bool | None = None
    if entry.pin:
        probe = git_stdout(
            entry.path, "--no-optional-locks", "cat-file", "-e", f"{entry.pin}^{{commit}}", timeout=_GIT_READ_TIMEOUT_S
        )
        pin_present = probe is not None
    side_exists = entry.side_path is not None and entry.side_path.exists()
    side_head: str | None = None
    side_clean: bool | None = None
    if side_exists and entry.side_path is not None:
        side_head, side_clean = _head_and_clean(entry.side_path)
    return RepoFacts(
        exists=True,
        head=head,
        clean=clean,
        pin_present=pin_present,
        side_exists=side_exists,
        side_head=side_head,
        side_clean=side_clean,
    )


def gather_local_facts(entries: list[PinnedRepo]) -> list[RepoFacts]:
    """Observe each entry's clone with read-only git; same order as ``entries``."""
    return [_gather_one(e) for e in entries]


def _at_pin(head: str | None, pin: str) -> bool:
    return head is not None and head.lower().startswith(pin.lower())


def classify_facts(entries: list[PinnedRepo], facts: list[RepoFacts]) -> list[RepoState]:
    """Apply the reuse rules to already-gathered facts.

    Never raises: a repository the reuse policy cannot serve comes back with
    ``blocking`` set to the reason, so the caller can report every blocker at
    once. Those are a ``url`` without a usable ``commit`` (kas would move the
    clone to a branch tip), an existing side checkout that is not clean at its
    pin, and a side-kind entry named in ``NO_SIDE_CHECKOUT``. The head, clean
    and pin-commit fields always describe the shared clone.
    """
    states: list[RepoState] = []
    for entry, fact in zip(entries, facts, strict=True):
        pin = entry.pin
        extra = {"head": fact.head, "clean": fact.clean, "pin_present": fact.pin_present}
        if not pin or not _HEX_PIN.fullmatch(pin):
            reason = "no commit pin" if not pin else "commit pin is not a hexadecimal commit id"
            states.append(RepoState(entry.name, entry.path, pin, "invalid", blocking=reason, **extra))
            continue
        if not fact.exists:
            states.append(RepoState(entry.name, entry.path, pin, "missing", **extra))
            continue
        if _at_pin(fact.head, pin) and fact.clean is True:
            states.append(RepoState(entry.name, entry.path, pin, "frozen", **extra))
            continue
        if fact.side_exists and not (_at_pin(fact.side_head, pin) and fact.side_clean is True):
            reason = (
                f"side checkout {entry.side_path} has local changes or is not at its pin; "
                "inspect it by hand, bakar never modifies it"
            )
            states.append(RepoState(entry.name, entry.path, pin, "side", blocking=reason, **extra))
            continue
        if entry.name in NO_SIDE_CHECKOUT:
            states.append(
                RepoState(entry.name, entry.path, pin, "side", blocking="off its pin or has local changes", **extra)
            )
            continue
        states.append(RepoState(entry.name, entry.path, pin, "side", entry.side_path, fact.side_exists, **extra))
    return states


def classify(repos: dict, workspace: Path) -> list[RepoState]:
    """Classify every pinned repository in a kas ``repos`` mapping."""
    entries = pinned_entries(repos, workspace)
    return classify_facts(entries, gather_local_facts(entries))


def blocking(states: list[RepoState]) -> list[RepoState]:
    """The states the reuse policy refuses to build with."""
    return [st for st in states if st.blocking is not None]


def _short(value: str | None) -> str:
    return value[:12] if value else "none"


def _clean_word(clean: bool | None) -> str:
    return "unknown" if clean is None else ("clean" if clean else "dirty")


def _presence_word(present: bool | None) -> str:
    return "unknown" if present is None else ("present" if present else "absent")


def _facts_text(st: RepoState) -> str:
    return (
        f"HEAD {_short(st.head)}, pin {_short(st.pin)}, {_clean_word(st.clean)}, "
        f"pin commit {_presence_word(st.pin_present)}"
    )


def render_refusal(states: list[RepoState], *, repair: str) -> str:
    """One message naming every blocking repository and the repair command."""
    blockers = blocking(states)
    noun = "repository blocks" if len(blockers) == 1 else "repositories block"
    lines = [f"{len(blockers)} {noun} this build:"]
    lines.extend(f"  {st.name}: {st.blocking}; {_facts_text(st)}" for st in blockers)
    lines.append(f"Move the clean clones to their pins with: {repair}")
    sides = [st.name for st in states if st.state == "side" and st.blocking is None]
    if sides:
        lines.append(f"Using side checkouts: {', '.join(sides)}")
    return "\n".join(lines)


def render_pin_table(states: list[RepoState]) -> list[str]:
    """One line per repository: kind, HEAD, pin, clean state, pin presence."""
    return [f"{st.name}: {st.state}, {_facts_text(st)}" for st in states]


def _kas_path(path: Path, workspace: Path) -> str:
    """Return ``path`` as the build's kas should name it.

    kas resolves a relative repo path against ``KAS_WORK_DIR``, which is the
    workspace on the host and ``/work`` inside kas-container, so a relative
    path names the same clone in both modes while a host-absolute one does
    not exist in the container. A repository outside the workspace has no
    such form and stays absolute.
    """
    # normpath first so a ``..`` component cannot pass the lexical check while
    # pointing outside; resolve second so a symlinked workspace still matches.
    lexical = (Path(os.path.normpath(workspace.absolute())), Path(os.path.normpath(path.absolute())))
    for base, target in (lexical, (workspace.resolve(), path.resolve())):
        if target.is_relative_to(base):
            return target.relative_to(base).as_posix()
    return str(path.absolute())


def reuse_overlay_text(states: list[RepoState], workspace: Path) -> str:
    """Render the kas overlay that keeps kas off every shared clone."""
    repos: dict[str, dict] = {}
    for st in states:
        if st.state == "frozen":
            # Without a path kas places a url-less repo at the dump file's
            # root, so name the clone classify actually checked.
            repos[st.name] = {"path": _kas_path(st.path, workspace), "url": None}
        elif st.state == "side" and st.side_path is not None:
            override: dict = {"path": _kas_path(st.side_path, workspace)}
            if st.side_exists:
                override["url"] = None
            repos[st.name] = override
    doc = {"header": {"version": _KAS_HEADER_VERSION}, "repos": repos}
    return yaml.safe_dump(doc, sort_keys=False)
