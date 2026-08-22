"""The reclaim gate: decide whether a source package may be deleted.

Every deletion in this project happens here and nowhere else, so the destructive
surface is one reviewable file. ``feed_retention`` also removes things, but only
inside the feed it owns; this module is the only one that touches a build tree.

The gate is TWO conditions and they are not redundant:

- the package's content is in the pool, and
- at least one rendered repository's metadata resolves to that content.

Pooled alone is insufficient. Content no repository references is held by the
feed and reachable by no client, so deleting the source loses it in practice
while every local check says the bytes are present.

Content, never file name. Two packages can share a name and differ in bytes, and
a name-keyed gate then deletes the variant that was never pooled because its
namesake was. That is how a package is lost while the check reports success, so
the gate hashes.

Three refusals are structural, and each one looks safe from the wrong angle:

- **An unreadable pool authorises nothing.** This one is worth stating precisely,
  because it is weaker than it looks: safety here comes from the gate requiring
  POSITIVE pool membership, so an unreadable pool retains everything whether it
  reports None or an empty set. The distinction the plan draws with
  ``pool_readable`` is diagnostic rather than protective - an operator seeing
  "not in the pool" against every one of 45,000 sources would conclude the
  consolidation pooled nothing, when the truth is that the pool could not be
  read. Both are refusals; only one of them points at the actual fault.
- **The feed is never its own source.** Tested on resolved paths, because the
  way the feed gets offered as a source is a search root holding a symlink to
  it, which a string comparison misses.
- **An NFS-exported path needs the operator to name it.** Another node can be
  building in that tree at this moment and nothing available locally can see it.
  On this host the shared workspace is exported read-write to both cluster nodes
  and holds three of the four build trees.

Nothing here deletes. This module plans; applying a plan is a separate,
explicitly requested step, so a preview and the deletion that follows it are the
same computation rather than two that can disagree.
"""

from __future__ import annotations

import gzip
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

_POOL_DIR = "_pkgs"
_INDEX = "repomd.xml"

# The pool names each entry by the sha256 of its contents, so a 64-hex stem IS
# the content identity. Matching the shape rather than trusting the extension
# keeps a stray file in the pool from being read as an entry.
_POOL_ENTRY = re.compile(r"^([0-9a-f]{64})\.rpm$")

# Which primary the index names. Read rather than globbed: the renderer leaves
# the previous primary in place on every re-render, so a directory holds several
# and a stale one still parses.
_PRIMARY_HREF = re.compile(r'href="repodata/([^"]*primary\.xml\.gz)"')

_POOL_HREF = re.compile(r'href="[^"]*_pkgs/[0-9a-f]{2}/([0-9a-f]{64})\.rpm"')

_READ_CHUNK = 1024 * 1024


@dataclass(frozen=True)
class Candidate:
    """One source package and what the gate decided about it."""

    path: Path
    size: int
    sha256: str | None = None
    reason: str | None = None
    """Why it was retained. None on an eligible candidate."""


@dataclass(frozen=True)
class ReclaimPlan:
    """What a reclaim would delete, and what it would keep and why."""

    eligible: list[Candidate] = field(default_factory=list)
    retained: list[Candidate] = field(default_factory=list)
    pool_readable: bool = True

    @property
    def reclaimable_bytes(self) -> int:
        return sum(c.size for c in self.eligible)


def sha256_file(path: Path) -> str:
    """Hash a file in chunks, since package files run to tens of megabytes."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_READ_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def pool_shas(channel_root: Path) -> set[str] | None:
    """Return the content identities the pool holds, or None if unreadable.

    None is a distinct answer from an empty set and the distinction is
    load-bearing - see the module docstring on why an unreadable pool must not
    read as an empty one.
    """
    pool = channel_root / _POOL_DIR
    if not pool.is_dir():
        return None
    try:
        return {match.group(1) for entry in pool.rglob("*.rpm") if (match := _POOL_ENTRY.match(entry.name))}
    except OSError:
        return None


def referenced_shas(channel_root: Path) -> set[str]:
    """Return every pool identity that live repository metadata resolves to.

    Live means named by ``repomd.xml``. A stale primary left behind by an
    earlier render is ignored, because a sha reachable only there is reachable
    by no client.
    """
    referenced: set[str] = set()
    for index in sorted(channel_root.rglob(f"repodata/{_INDEX}")):
        try:
            repomd = index.read_text()
        except OSError:
            continue
        match = _PRIMARY_HREF.search(repomd)
        if match is None:
            continue
        primary = index.parent / match.group(1)
        try:
            with gzip.open(primary) as handle:
                body = handle.read().decode()
        except OSError, ValueError, EOFError:
            continue
        referenced.update(_POOL_HREF.findall(body))
    return referenced


def _resolved_under(path: Path, parent: Path) -> bool:
    """True when ``path`` resolves inside ``parent``, symlinks included."""
    try:
        return path.resolve().is_relative_to(parent.resolve())
    except OSError:
        # An unresolvable path is treated as inside: refusing to delete
        # something we cannot locate is the safe direction.
        return True


def plan_reclaim(
    sources: Iterable[Path],
    *,
    channel_root: Path,
    feed_root: Path,
    exported: Iterable[Path] = (),
    allow_exported: Iterable[Path] = (),
) -> ReclaimPlan:
    """Classify each source package without touching any of them.

    ``exported`` names paths under an NFS export; ``allow_exported`` names the
    ones the operator has explicitly accepted. A path under the former and not
    the latter is retained.
    """
    pooled = pool_shas(channel_root)
    if pooled is None:
        return ReclaimPlan(
            retained=[
                Candidate(
                    path=source,
                    size=_size(source),
                    reason=(
                        f"the pool under {channel_root / _POOL_DIR} could not be read, so "
                        "nothing can be shown to be servable; an unanswerable check is "
                        "not a passing check"
                    ),
                )
                for source in sources
            ],
            pool_readable=False,
        )

    referenced = referenced_shas(channel_root)
    exported_roots = [Path(p) for p in exported]
    allowed_roots = [Path(p) for p in allow_exported]

    eligible: list[Candidate] = []
    retained: list[Candidate] = []

    for source in sources:
        size = _size(source)

        if _resolved_under(source, feed_root):
            retained.append(
                Candidate(
                    path=source,
                    size=size,
                    reason=f"resolves inside the feed at {feed_root}; the feed is not a source",
                )
            )
            continue

        blocking = _blocking_export(source, exported_roots, allowed_roots)
        if blocking is not None:
            retained.append(
                Candidate(
                    path=source,
                    size=size,
                    reason=(
                        f"lies under the NFS-exported path {blocking}, where another node "
                        "may be building; name that path explicitly to allow it"
                    ),
                )
            )
            continue

        digest = sha256_file(source)
        if digest not in pooled:
            retained.append(
                Candidate(
                    path=source,
                    size=size,
                    sha256=digest,
                    reason="its content is not in the pool, so the feed cannot serve it",
                )
            )
            continue
        if digest not in referenced:
            retained.append(
                Candidate(
                    path=source,
                    size=size,
                    sha256=digest,
                    reason=(
                        "its content is pooled but no repository's live metadata "
                        "resolves to it, so no client can reach it"
                    ),
                )
            )
            continue

        eligible.append(Candidate(path=source, size=size, sha256=digest))

    return ReclaimPlan(eligible=eligible, retained=retained, pool_readable=True)


def _blocking_export(source: Path, exported: list[Path], allowed: list[Path]) -> Path | None:
    """Return the export covering ``source`` when it has not been allowed."""
    for root in exported:
        if not _resolved_under(source, root):
            continue
        if any(_resolved_under(source, ok) for ok in allowed):
            return None
        return root
    return None


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0
