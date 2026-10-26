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
import subprocess
import sys
import time
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import yaml

from bakar.gitutil import git_stdout

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence
    from typing import IO

RepoStateKind = Literal["frozen", "missing", "side", "invalid"]

# The build puts <workspace>/bitbake/bin first on PATH and oe-core finds bitbake
# as ../bitbake, so a side checkout of either is never the copy the build runs.
# One exception: a diverged openembedded-core may take a side checkout when
# pairing is enabled and bitbake is frozen at its pin, because the side checkout's
# sibling bitbake link then resolves to that same frozen clone.
NO_SIDE_CHECKOUT = ("bitbake", "openembedded-core")

# kas accepts an abbreviated pin and a SHA-256 commit id as well as a SHA-1 one.
_HEX_PIN = re.compile(r"[0-9a-fA-F]{4,64}")
# bakar sync moves a clone only to a full SHA-1 pin, so only those can take its repair line.
_SYNCABLE_PIN = re.compile(r"[0-9a-fA-F]{40}")
# A kas repository name or path is untrusted text; an ESC sequence in it could
# erase or forge earlier report lines.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

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
    paired_bitbake: Path | None = None


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


# A sync takes this exclusively and without waiting; every reuse build holds it
# shared for its whole run, so a sync never moves a clone under a live build.
IN_USE_LOCK_NAME = "repos-in-use.lock"

# Kept open for the life of the process: closing the file would drop the flock.
_IN_USE_HELD: dict[Path, IO[str]] = {}


def hold_workspace_in_use(workspace: Path, *, wait_limit_s: float = LOCK_WAIT_LIMIT_S) -> None:
    """Hold the workspace's whole-run lock in shared mode until the process exits.

    Shared holders never block each other; only a sync, which takes the lock
    exclusively, does. A killed process releases it with its file descriptor.
    A second call for the same workspace is a no-op.

    Raises RuntimeError naming the lock file when a sync holds it past
    ``wait_limit_s``.
    """
    key = workspace.resolve()
    if key in _IN_USE_HELD:
        return
    lock_dir = workspace / ".bakar"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / IN_USE_LOCK_NAME
    fh = lock_path.open("a")
    start = time.monotonic()
    noticed = False
    try:
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_SH | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                waited = time.monotonic() - start
                if waited >= wait_limit_s:
                    raise RuntimeError(
                        f"gave up after waiting {waited:.0f}s for {lock_path}: a bakar sync still holds it"
                    ) from None
                if not noticed and waited >= _LOCK_NOTICE_AFTER_S:
                    print(f"waiting for {lock_path}: a bakar sync holds it", file=sys.stderr)
                    noticed = True
                time.sleep(_LOCK_POLL_S)
    except BaseException:
        fh.close()
        raise
    _IN_USE_HELD[key] = fh


def holds_in_use(workspace: Path) -> bool:
    """Whether this process holds ``workspace``'s whole-run lock."""
    return workspace.resolve() in _IN_USE_HELD


# Takes the lock shared, then sleeps until the process behind the pidfd exits.
_IN_USE_HOLDER = (
    "import fcntl, select, sys\n"
    "fh = open(sys.argv[1], 'a')\n"
    "fcntl.flock(fh, fcntl.LOCK_SH)\n"
    "select.select([int(sys.argv[2])], [], [])\n"
)


def keep_in_use_until_exit(workspace: Path, child: subprocess.Popen) -> None:
    """Keep the whole-run lock shared until ``child`` exits, even if this process dies first.

    The front end's own descriptor is not inherited by the scoped kas and
    bitbake children, so a front end killed without ``--detach`` would drop the
    lock while its build still runs. A detached holder in its own session keeps
    the lock for the life of ``child`` instead. Handing the descriptor to the
    build itself would also pin the lock to any bitbake server that outlives it.

    ``child`` must not have been waited on yet, so the pidfd opened here names
    that process and no later one: a holder that polled the PID number instead
    would keep the lock for whatever process the kernel reuses it for. Best
    effort: if the pidfd or the holder cannot be had, the front end's own hold
    is all there is.
    """
    if not holds_in_use(workspace):
        return
    lock_path = workspace / ".bakar" / IN_USE_LOCK_NAME
    try:
        pidfd = os.pidfd_open(child.pid)
    except OSError:
        return
    try:
        with suppress(OSError):
            subprocess.Popen(
                [sys.executable, "-I", "-c", _IN_USE_HOLDER, str(lock_path), str(pidfd)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
                pass_fds=(pidfd,),
            )
    finally:
        os.close(pidfd)


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


def _is_frozen(entry: PinnedRepo, fact: RepoFacts) -> bool:
    pin = entry.pin
    return bool(pin and _HEX_PIN.fullmatch(pin) and fact.exists and _at_pin(fact.head, pin) and fact.clean is True)


def classify_facts(
    entries: list[PinnedRepo],
    facts: list[RepoFacts],
    *,
    pair_core_with_bitbake: bool = False,
) -> list[RepoState]:
    """Apply the reuse rules to already-gathered facts.

    Never raises: a repository the reuse policy cannot serve comes back with
    ``blocking`` set to the reason, so the caller can report every blocker at
    once. Those are a ``url`` without a usable ``commit`` (kas would move the
    clone to a branch tip), an existing side checkout that is not clean at its
    pin, and a side-kind entry named in ``NO_SIDE_CHECKOUT``. The head, clean
    and pin-commit fields always describe the shared clone.

    With ``pair_core_with_bitbake`` a diverged ``openembedded-core`` is not
    blocking when the ``bitbake`` entry is frozen: it takes a side checkout and
    records the frozen clone as ``paired_bitbake``. A diverged ``bitbake``
    always blocks.
    """
    frozen_bitbake: Path | None = None
    if pair_core_with_bitbake:
        for entry, fact in zip(entries, facts, strict=True):
            if entry.name == "bitbake" and _is_frozen(entry, fact):
                frozen_bitbake = entry.path
                break
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
            states.append(
                RepoState(
                    entry.name, entry.path, pin, "side", entry.side_path, fact.side_exists, blocking=reason, **extra
                )
            )
            continue
        paired = entry.name == "openembedded-core" and frozen_bitbake is not None
        if entry.name in NO_SIDE_CHECKOUT and not paired:
            states.append(
                RepoState(entry.name, entry.path, pin, "side", blocking="off its pin or has local changes", **extra)
            )
            continue
        states.append(
            RepoState(
                entry.name,
                entry.path,
                pin,
                "side",
                entry.side_path,
                fact.side_exists,
                **extra,
                paired_bitbake=frozen_bitbake if paired else None,
            )
        )
    return states


def classify(repos: dict, workspace: Path, *, pair_core_with_bitbake: bool = False) -> list[RepoState]:
    """Classify every pinned repository in a kas ``repos`` mapping."""
    entries = pinned_entries(repos, workspace)
    return classify_facts(entries, gather_local_facts(entries), pair_core_with_bitbake=pair_core_with_bitbake)


def ensure_core_bitbake_link(state: RepoState) -> None:
    """Make ``<side checkout>/../bitbake`` resolve to the workspace's bitbake clone.

    oe-buildenv-internal finds bitbake at ``$OEROOT/../bitbake`` and resolves it
    with ``readlink -f``, so the link sits beside the side checkout. An existing
    entry that resolves anywhere else is never replaced.

    The target is computed between resolved paths: a symlinked ``.bakar/pins``
    would otherwise make a lexical ``../../../bitbake`` land beside the
    symlink's target, and every later build would refuse that link. A link that
    still does not resolve to the clone once created is removed and refused.
    """
    if state.side_path is None or state.paired_bitbake is None:
        raise RepoReuseError(f"{state.name} has no paired bitbake to link")
    link_dir = state.side_path.parent
    link = link_dir / "bitbake"
    want = os.path.realpath(state.paired_bitbake)
    if os.path.lexists(link):
        if os.path.realpath(link) == want:
            return
        raise RepoReuseError(
            f"{link} already exists and does not resolve to {state.paired_bitbake}; bakar never replaces it"
        )
    link_dir.mkdir(parents=True, exist_ok=True)
    os.symlink(os.path.relpath(want, os.path.realpath(link_dir)), link)
    if os.path.realpath(link) != want:
        got = os.path.realpath(link)
        link.unlink()
        raise RepoReuseError(f"{link} resolved to {got}, not {state.paired_bitbake}; bakar removed it")


def blocking(states: list[RepoState]) -> list[RepoState]:
    """The states the reuse policy refuses to build with."""
    return [st for st in states if st.blocking is not None]


def _short(value: str | None) -> str:
    return value[:12] if value else "none"


def _clean_word(*, clean: bool | None) -> str:
    return "unknown" if clean is None else ("clean" if clean else "dirty")


def _presence_word(*, present: bool | None) -> str:
    return "unknown" if present is None else ("present" if present else "absent")


def _facts_text(st: RepoState) -> str:
    return (
        f"HEAD {_short(st.head)}, pin {_short(st.pin)}, {_clean_word(clean=st.clean)}, "
        f"pin commit {_presence_word(present=st.pin_present)}"
    )


def sync_can_fix(st: RepoState) -> bool:
    """Whether ``bakar sync`` moving the shared clone to its pin clears this blocker.

    Sync moves a clone that exists, has a full 40-hex pin and is clean but off
    that pin; a dirty clone, an unreadable one and an entry with an abbreviated,
    SHA-256 or unusable pin are left alone, so those need a hand edit.
    """
    return (
        st.state != "invalid"
        and st.pin is not None
        and _SYNCABLE_PIN.fullmatch(st.pin) is not None
        and st.head is not None
        and st.clean is True
        and not _at_pin(st.head, st.pin)
    )


def printable(value: object) -> str:
    """``value`` as text with every ASCII control character replaced by ``?``."""
    return _CONTROL_CHARS.sub("?", str(value))


def render_blockers(states: list[RepoState]) -> list[str]:
    """One line per blocking repository: name, clone path, why, then its HEAD, pin, clean state and pin presence."""
    return [
        f"  {printable(st.name)} ({printable(st.path)}): {printable(st.blocking)}; {printable(_facts_text(st))}"
        for st in blocking(states)
    ]


def render_refusal(
    states: list[RepoState],
    *,
    repair: str,
    extra_overlays: Sequence[str] = (),
    main_pins: Mapping[str, str | None] | None = None,
) -> str:
    """One message naming every blocking repository and what clears it.

    ``extra_overlays`` names the user overlays merged after the main kas file.
    ``bakar sync`` reads only the main file, so when one of them re-pins a
    repository the sync moves it to the main file's pin, not the build's.
    ``main_pins`` maps each repository to the pin the main file alone sets;
    ``repair`` is withheld only when a blocker sync could move has a different
    pin there, or when overlays are present and ``main_pins`` is unknown.
    """
    blockers = blocking(states)
    noun = "repository blocks" if len(blockers) == 1 else "repositories block"
    lines = [f"{len(blockers)} {noun} this build:", *render_blockers(states)]
    syncable = [st for st in blockers if sync_can_fix(st)]
    if syncable:
        repinned = [] if main_pins is None else [st for st in syncable if main_pins.get(st.name) != st.pin]
        if extra_overlays and (main_pins is None or repinned):
            names = ", ".join(printable(name) for name in extra_overlays)
            pinned = ", ".join(f"{printable(st.name)} at {_short(st.pin)}" for st in repinned)
            what = f", and they pin {pinned}" if pinned else ""
            lines.append(
                f"This build also merges {names}, which bakar sync does not read{what}: fix the pin in the "
                "overlay that sets it, or move the clean clones to their pins by hand"
            )
        else:
            lines.append(f"Move the clean clones to their pins with: {printable(repair)}")
    by_hand = [printable(st.name) for st in blockers if not sync_can_fix(st)]
    if by_hand:
        lines.append(f"bakar sync leaves these alone, so fix them by hand: {', '.join(by_hand)}")
    sides = [printable(st.name) for st in states if st.state == "side" and st.blocking is None]
    if sides:
        lines.append(f"Using side checkouts: {', '.join(sides)}")
    return "\n".join(lines)


def render_pin_table(states: list[RepoState]) -> list[str]:
    """One line per repository: kind, HEAD, pin, clean state, pin presence."""
    return [f"{printable(st.name)}: {st.state}, {printable(_facts_text(st))}" for st in states]


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
