"""Decide which mirror payloads to link into the downloads directory, then link them.

Planning is pure: the only filesystem contact is one ``os.scandir`` of the
downloads directory (names only, no per-file stat) and the bounded mirror
listing supplied by ``lister``. No mirror file is statted, opened or read and
no link is followed. Applying creates links with ``os.symlink`` only: it never
replaces, removes, renames or chmods an entry and writes nothing else.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from bakar.mounts import list_regular_files
from bakar.steps.cache_queue import is_unsafe_name

_DONE_SUFFIX = ".done"
_SKIP_SUFFIXES = (".done", ".tmp", ".lock")
_GIT_MIRROR_PREFIXES = ("git2_", "gitshallow_")
# Tarballs BitBake extracts with no checksum (supports_checksum is False), so a
# link would put unverified bytes into a build. Link-only: push and prune keep
# the two-prefix list above. svn, cvs and bzr tarballs carry no fixed prefix
# (<module>_<host>_<path>_<rev>.tar.gz) and cannot be named here.
_UNCHECKED_TARBALL_PREFIXES = ("gitsmshallow_", "gitannexshallow_", "hg_", "repo_")
# Names BitBake creates as DL_DIR subdirectories; a link by this name makes
# mkdirhier fail with ENOTDIR and every fetch of that kind fail with it.
_RESERVED_NAMES = frozenset({"git2", "svn", "cvs", "bzr", "hg", "repo", "p4", "uninative", "npm2", "gomod"})
_DEFAULT_DEADLINE_S = 20.0

EMPTY_HINT = "lists no regular files, as a share that is not mounted does"

KEPT_NO_STAMP = "no stamp"
KEPT_GIT_TARBALL = "git mirror tarball"
KEPT_UNCHECKED = "unchecked tarball"
KEPT_RESERVED = "reserved name"
KEPT_ORPHAN_STAMP = "local stamp without payload"
KEPT_UNSAFE = "unsafe name"
KEPT_PRESENT = "already present"
KEPT_LINK_FAILED = "link failed"

Lister = Callable[..., dict[Path, list[str] | None]]


@dataclass(frozen=True)
class LinkItem:
    name: str
    target: Path


@dataclass
class LinkPlan:
    items: list[LinkItem] = field(default_factory=list)
    kept: dict[str, int] = field(default_factory=dict)
    unlisted: list[Path] = field(default_factory=list)
    empty: list[Path] = field(default_factory=list)
    examined: int = 0
    listed: int = 0


@dataclass
class ApplyResult:
    linked: int = 0
    linked_names: list[str] = field(default_factory=list)
    kept: dict[str, int] = field(default_factory=dict)
    first_error: str = ""


def _local_names(dl_dir: Path) -> set[str]:
    with os.scandir(dl_dir) as it:
        return {e.name for e in it}


def plan_links(
    dl_dir: Path,
    mirror_dirs: Sequence[Path],
    *,
    lister: Lister = list_regular_files,
    deadline_s: float = _DEFAULT_DEADLINE_S,
) -> LinkPlan:
    """Plan the links to create; ``mirror_dirs`` must already be known ready."""
    plan = LinkPlan()
    present = _local_names(dl_dir)
    listings = lister(list(mirror_dirs), deadline_s=deadline_s)
    seen: set[str] = set()
    for mdir in mirror_dirs:
        names = listings.get(mdir)
        if names is None:
            plan.unlisted.append(mdir)
            continue
        plan.listed += 1
        if not names:
            plan.empty.append(mdir)
        stamps = set(names)
        for name in sorted(names):
            if name.endswith(_SKIP_SUFFIXES) or name in seen:
                continue
            seen.add(name)
            plan.examined += 1
            if is_unsafe_name(name):
                reason = KEPT_UNSAFE
            elif name in _RESERVED_NAMES:
                reason = KEPT_RESERVED
            elif name.startswith(_GIT_MIRROR_PREFIXES):
                reason = KEPT_GIT_TARBALL
            elif name.startswith(_UNCHECKED_TARBALL_PREFIXES):
                reason = KEPT_UNCHECKED
            elif name + _DONE_SUFFIX not in stamps:
                reason = KEPT_NO_STAMP
            elif name in present:
                reason = KEPT_PRESENT
            elif name + _DONE_SUFFIX in present:
                reason = KEPT_ORPHAN_STAMP
            else:
                plan.items.append(LinkItem(name, mdir / name))
                continue
            plan.kept[reason] = plan.kept.get(reason, 0) + 1
    return plan


def apply_links(plan: LinkPlan, dl_dir: Path) -> ApplyResult:
    """Create each planned link; an existing name is never touched."""
    result = ApplyResult()
    for item in plan.items:
        try:
            os.symlink(item.target, dl_dir / item.name)
        except FileExistsError:
            reason = KEPT_PRESENT
        except OSError as exc:
            reason = KEPT_LINK_FAILED
            result.first_error = result.first_error or exc.strerror or type(exc).__name__
        else:
            result.linked += 1
            result.linked_names.append(item.name)
            continue
        result.kept[reason] = result.kept.get(reason, 0) + 1
    return result


def render_report(plan: LinkPlan, result: ApplyResult, *, apply: bool) -> str:
    lines = [f"{plan.listed} mirror directories listed, {plan.examined} payloads examined"]
    lines.extend(f"unlisted: {mdir}" for mdir in plan.unlisted)
    lines.extend(f"empty: {mdir} ({EMPTY_HINT})" for mdir in plan.empty)
    if apply:
        lines.append(f"linked {result.linked} file(s)")
    else:
        lines.append(f"would link {len(plan.items)} file(s)")
    kept = dict(plan.kept)
    for reason, n in result.kept.items():
        kept[reason] = kept.get(reason, 0) + n
    lines.extend(f"kept {kept[reason]}: {reason}" for reason in sorted(kept))
    if apply and result.first_error:
        lines.append(f"first link error: {result.first_error}")
    if apply:
        lines.append("applied")
    else:
        lines.append("dry run: nothing was linked (pass --apply to link)")
    return "\n".join(lines)
