"""Decide which mirror payloads to link into the downloads directory.

Pure planning: the only filesystem contact is one ``os.scandir`` of the
downloads directory (names only, no per-file stat) and the bounded mirror
listing supplied by ``lister``. No mirror file is statted, opened or read and
no link is followed.
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
_DEFAULT_DEADLINE_S = 20.0

KEPT_NO_STAMP = "no stamp"
KEPT_GIT_TARBALL = "git mirror tarball"
KEPT_UNSAFE = "unsafe name"
KEPT_PRESENT = "already present"

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
    examined: int = 0


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
        stamps = set(names)
        for name in sorted(names):
            if name.endswith(_SKIP_SUFFIXES) or name in seen:
                continue
            seen.add(name)
            plan.examined += 1
            if is_unsafe_name(name):
                reason = KEPT_UNSAFE
            elif name.startswith(_GIT_MIRROR_PREFIXES):
                reason = KEPT_GIT_TARBALL
            elif name + _DONE_SUFFIX not in stamps:
                reason = KEPT_NO_STAMP
            elif name in present:
                reason = KEPT_PRESENT
            else:
                plan.items.append(LinkItem(name, mdir / name))
                continue
            plan.kept[reason] = plan.kept.get(reason, 0) + 1
    return plan
