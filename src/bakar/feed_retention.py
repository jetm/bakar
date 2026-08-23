"""Retention inside the feed: drop old snapshots, stale metadata, orphan pool.

Three things accumulate, and only one of them is the one people expect.

Snapshots are the obvious growth: every sync renders one, and nothing removes
them. The pool grows with them, since a snapshot's packages are pooled content
that outlives the snapshot's own repository.

The unobvious one is repository metadata. ``render-pool-local.py`` writes a
checksum-named ``primary``/``other``/``filelists`` triple on every render and
rewrites ``repomd.xml`` to name the new one - but it never removes the previous
triple. So a repository re-rendered into the same subpath keeps every generation
it ever had. Measured on the live feed: ``sdk/all/repodata`` held 14 metadata
files of which 3 were live. That grows on every sync even when no package
changed, and none of it is reachable by a client.

Which is why nothing here identifies live metadata by globbing. A stale
``*-primary.xml.gz`` parses perfectly and names real packages; it is dead only
because ``repomd.xml`` stopped naming it. Following the index is the only way to
tell the two apart.

**Unreadable metadata refuses; it never reads as "references nothing".** This is
the load-bearing rule and it applies to the index AND to the primary underneath
it, because the two fail differently and the primary's failure is the dangerous
one. A repository whose index parses but whose primary cannot be decompressed
still serves that index to clients, so treating its packages as unreferenced
orphans deletes content the feed is actively publishing - and the post-run audit,
reading the same broken primary, would report success. Both cases are therefore
collected and reported, and any unreadable primary suppresses pool reclamation
for the whole run. An unanswerable check is not a passing check.

Two decompression failures matter and only one of them looks like an error.
``gzip.BadGzipFile`` subclasses ``OSError``, so a primary compressed with
something other than gzip is caught by an ordinary handler and silently skipped.
``zlib.error`` does NOT subclass ``OSError`` - a file with correct gzip magic and
a corrupt deflate payload raises straight past every ``OSError`` handler and
aborts the run mid-way. :func:`_read_primary` catches both.

Pool orphans are computed against the POST-removal reference set, not measured
after the fact, so a preview and the run that follows it are one computation
rather than two that can disagree - the same property
:mod:`bakar.feed_reclaim` holds for build trees.

Scope is deliberately asymmetric between the two halves. References are gathered
across the whole feed root, because a retained repository in another release may
resolve into this channel's pool and a per-channel scan would not see it.
Deletion is confined to the channel the caller named: sweeping metadata
feed-wide would have ``gc --channel edge`` mutating channels the invocation never
mentioned.
"""

from __future__ import annotations

import gzip
import json
import re
import zlib
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

_POINTER = "snapshots-latest.json"
_SNAPSHOTS_DIR = "snapshots"
_REPODATA = "repodata"
_INDEX = "repomd.xml"
_POOL_DIR = "_pkgs"

# What repomd.xml names, so the live triple can be told from its predecessors.
# Relative to the repodata directory, which is how the renderer writes them.
_METADATA_HREF = re.compile(r'href="repodata/([^"]+)"')

# A package's location inside primary.xml, relative to the REPOSITORY root - not
# to repodata/. Verified against the live feed: sdk/all's entries read
# `../../_pkgs/<aa>/<sha>.rpm`, which resolves from sdk/all/ and not from
# sdk/all/repodata/.
_LOCATION_HREF = re.compile(r'<location href="([^"]+)"')

# The shape bakar.feed.snapshot_id mints. Matched rather than parsed, so a
# foreign name is classified without raising - but it MUST be classified, because
# a name that merely sorts still occupies a slot in the keep window. Any name
# starting with a letter sorts after every timestamp ("k" > "2"), so an
# interrupted sync's leftover directory would otherwise evict the newest real
# snapshot.
_SNAPSHOT_ID = re.compile(r"^\d{8}T\d{6}Z$")


@dataclass(frozen=True)
class MetadataScan:
    """One pass over a tree's live metadata, with what it could not read."""

    pool_paths: set[Path] = field(default_factory=set)

    unreadable_indexes: list[Path] = field(default_factory=list)
    """Repositories whose ``repomd.xml`` could not be read, so nothing about
    them is known."""

    unreadable_primaries: list[Path] = field(default_factory=list)
    """Repositories whose index parsed but whose package list could not be
    decompressed. These still serve, so their packages must not be reclaimed."""

    @property
    def complete(self) -> bool:
        """True when every live primary under the tree was read.

        Only a complete scan may authorise a pool deletion.
        """
        return not self.unreadable_primaries


@dataclass(frozen=True)
class RetentionPlan:
    """What a retention run would remove, and what it holds back."""

    channel_root: Path

    kept_snapshots: list[str] = field(default_factory=list)
    removed_snapshots: list[str] = field(default_factory=list)

    pinned: str | None = None
    """The snapshot the pointer names. Never appears in ``removed_snapshots``."""

    stale_metadata: list[Path] = field(default_factory=list)
    orphan_pool: list[Path] = field(default_factory=list)

    unreadable_indexes: list[Path] = field(default_factory=list)
    """Repositories skipped because their index could not be read."""

    unreadable_primaries: list[Path] = field(default_factory=list)
    """Repositories whose package list could not be read. Non-empty means pool
    reclamation was suppressed for the whole run."""

    foreign_snapshot_entries: list[str] = field(default_factory=list)
    """Names under ``snapshots/`` that are not snapshot ids - a partial sync, or
    something an operator put there. Never removed, and never counted against
    the keep window."""

    @property
    def pool_reclaim_suppressed(self) -> bool:
        return bool(self.unreadable_primaries)

    @property
    def reclaimable_bytes(self) -> int:
        """Size of everything this plan would remove, measured NOW.

        Only meaningful before the run - it stats live paths, so calling it
        afterwards reports what is left rather than what went. Read
        ``RetentionResult.freed_bytes`` for what a run actually freed.
        """
        paths = [*self.stale_metadata, *self.orphan_pool]
        return sum(_size(p) for p in paths) + sum(
            _tree_size(self.channel_root / _SNAPSHOTS_DIR / snap) for snap in self.removed_snapshots
        )

    @property
    def is_empty(self) -> bool:
        """True when there is nothing to do.

        Reported explicitly so a no-op run says so, rather than printing a
        success with no detail and leaving the operator unsure whether it looked.
        """
        return not (self.removed_snapshots or self.stale_metadata or self.orphan_pool)


@dataclass(frozen=True)
class RetentionResult:
    """What a retention run did, or would have done while only previewing."""

    applied: bool
    removed_snapshots: list[str] = field(default_factory=list)
    removed_metadata: list[Path] = field(default_factory=list)
    removed_pool: list[Path] = field(default_factory=list)

    failed_snapshots: list[str] = field(default_factory=list)
    """Snapshots the plan approved that could not be removed. Absent from
    ``removed_snapshots`` and not counted as freed."""

    dangling: list[tuple[Path, Path]] = field(default_factory=list)
    """(index, missing target) pairs found by the post-run audit. Must be empty."""

    unreadable_primaries: list[Path] = field(default_factory=list)
    """Primaries the post-run audit could not read. The audit cannot vouch for
    these repositories, so an empty ``dangling`` alongside a non-empty list here
    is NOT a clean result."""

    freed_bytes: int = 0

    @property
    def audit_clean(self) -> bool:
        """True only when the audit both found nothing wrong and could see."""
        return not self.dangling and not self.unreadable_primaries


def pinned_snapshot(channel_root: Path) -> str | None:
    """Return the snapshot id the pointer names, or None when there is none.

    A malformed pointer answers None rather than raising, but see
    :func:`plan_retention` - None here does not mean "nothing is pinned", it
    means the pin is unknown, and those must not be treated alike.
    """
    try:
        pointer = json.loads((channel_root / _POINTER).read_text(encoding="utf-8"))
    except OSError, ValueError:
        return None
    identifier = pointer.get("id") if isinstance(pointer, dict) else None
    return identifier if isinstance(identifier, str) else None


def pointer_present(channel_root: Path) -> bool:
    """True when a pointer file exists, regardless of whether it parses."""
    return (channel_root / _POINTER).is_file()


def list_snapshots(channel_root: Path) -> list[str]:
    """Return the snapshot ids present, oldest first.

    Sorted lexicographically, which is chronological because
    :func:`bakar.feed.snapshot_id` mints a zero-padded UTC stamp.

    Only names matching that shape are returned, and symlinks are excluded. A
    foreign name is not a snapshot and must not consume a slot in the keep
    window; a symlinked snapshot is not ours to delete, since removing it would
    walk out of the feed into whatever it points at. Both are reported by
    :func:`foreign_snapshot_entries` rather than silently dropped.
    """
    snapshots = channel_root / _SNAPSHOTS_DIR
    if not snapshots.is_dir():
        return []
    return sorted(
        entry.name
        for entry in snapshots.iterdir()
        if entry.is_dir() and not entry.is_symlink() and _SNAPSHOT_ID.match(entry.name)
    )


def foreign_snapshot_entries(channel_root: Path) -> list[str]:
    """Return names under ``snapshots/`` that :func:`list_snapshots` excluded."""
    snapshots = channel_root / _SNAPSHOTS_DIR
    if not snapshots.is_dir():
        return []
    return sorted(
        entry.name
        for entry in snapshots.iterdir()
        if entry.is_symlink() or not (entry.is_dir() and _SNAPSHOT_ID.match(entry.name))
    )


def live_metadata(repodata: Path) -> set[Path] | None:
    """Return the files ``repomd.xml`` names, plus the index itself.

    None when the index is absent or unreadable. That is a refusal, not an empty
    answer: an unreadable index makes every sibling look unreferenced, and
    sweeping on that reading would delete the whole repository's metadata.

    ``encoding`` is pinned because ``repomd.xml`` is XML and therefore UTF-8.
    Letting it default would decode the file through the ambient locale, which
    additionally raises ``UnicodeDecodeError`` - a ``ValueError``, so it would
    escape an ``OSError``-only handler and abort the run.
    """
    index = repodata / _INDEX
    try:
        body = index.read_text(encoding="utf-8")
    except OSError, ValueError:
        return None
    return {index, *(repodata / name for name in _METADATA_HREF.findall(body))}


def _read_primary(path: Path) -> str | None:
    """Decompress a primary, or None when it cannot be read.

    ``zlib.error`` is caught explicitly because it does NOT subclass
    ``OSError``: gzip raises it for a file with correct magic and a corrupt
    payload, which would otherwise escape every handler here. See the module
    docstring on why that difference matters.
    """
    try:
        with gzip.open(path) as handle:
            return handle.read().decode("utf-8")
    except OSError, ValueError, EOFError, zlib.error:
        return None


def _repodata_dirs(root: Path, *, skip: Iterable[Path] = ()) -> list[Path]:
    """Return every ``repodata`` directory under ``root``, skipping some trees.

    Found by directory name, NOT by globbing for ``repomd.xml``. A directory
    holding metadata with no index is the case worth seeing - every file in it
    looks unreferenced - and globbing for the index makes exactly that directory
    invisible, so it is neither swept nor reported.
    """
    skipped = [p.resolve() for p in skip]
    found: list[Path] = []
    for repodata in sorted(root.rglob(_REPODATA)):
        if not repodata.is_dir():
            continue
        if any(_resolves_inside(repodata, blocked, unknown=False) for blocked in skipped):
            continue
        found.append(repodata)
    return found


def stale_metadata(root: Path, *, skip: Iterable[Path] = ()) -> tuple[list[Path], list[Path]]:
    """Return (sweepable metadata, repositories whose index could not be read).

    Sweepable means present in a ``repodata`` directory and not named by that
    directory's ``repomd.xml``. Both halves are returned because a skipped
    repository is a gap in the sweep an operator should see reported, not a
    silent omission.

    ``root`` is the tree to DELETE from and should be the channel the caller
    named, not the whole feed - see the module docstring on scope.
    """
    sweepable: list[Path] = []
    unreadable: list[Path] = []
    for repodata in _repodata_dirs(root, skip=skip):
        live = live_metadata(repodata)
        if live is None:
            unreadable.append(repodata / _INDEX)
            continue
        sweepable.extend(sorted(entry for entry in repodata.iterdir() if entry.is_file() and entry not in live))
    return sweepable, unreadable


def scan_live_metadata(root: Path, *, skip: Iterable[Path] = ()) -> MetadataScan:
    """Resolve every pool file live metadata under ``root`` points at.

    Resolved paths rather than content digests, so a reference that crosses into
    another release's pool is followed to the file it actually names instead of
    being matched by digest against the wrong pool.

    ``skip`` names trees to treat as already gone, which is what lets a plan
    describe the post-removal state without removing anything first.

    Anything unreadable is recorded rather than skipped - see
    :attr:`MetadataScan.complete`.
    """
    pool_paths: set[Path] = set()
    unreadable_indexes: list[Path] = []
    unreadable_primaries: list[Path] = []

    for repodata in _repodata_dirs(root, skip=skip):
        live = live_metadata(repodata)
        if live is None:
            unreadable_indexes.append(repodata / _INDEX)
            continue
        # Resolved once per repository rather than once per package reference:
        # a populated repository names thousands, and the repo root is fixed.
        try:
            repo_root = repodata.parent.resolve()
        except OSError:
            unreadable_indexes.append(repodata / _INDEX)
            continue
        for primary in sorted(p for p in live if "primary" in p.name):
            body = _read_primary(primary)
            if body is None:
                unreadable_primaries.append(primary)
                continue
            for href in _LOCATION_HREF.findall(body):
                with suppress(OSError):
                    pool_paths.add((repo_root / href).resolve())

    return MetadataScan(
        pool_paths=pool_paths,
        unreadable_indexes=unreadable_indexes,
        unreadable_primaries=unreadable_primaries,
    )


def pool_entries(channel_root: Path) -> list[Path]:
    """Return the channel's pool files, sorted, as resolved paths.

    The pool directory is resolved ONCE and walked from there, so the entries
    come back already resolved and need no per-file ``resolve()`` to compare
    against the reference set. On the live feed that is one syscall chain instead
    of 44,765, over NFS.

    A symlinked entry is excluded rather than resolved: it is a pointer to
    content that lives somewhere else, and deleting it is not this module's call.
    """
    pool = channel_root / _POOL_DIR
    if not pool.is_dir():
        return []
    try:
        resolved = pool.resolve()
    except OSError:
        return []
    return sorted(entry for entry in resolved.rglob("*.rpm") if entry.is_file() and not entry.is_symlink())


def plan_retention(
    channel_root: Path,
    *,
    feed_root: Path,
    keep: int,
) -> RetentionPlan:
    """Decide what a retention run would remove, touching nothing.

    ``keep`` counts snapshots retained by age. The pinned snapshot is retained
    on top of that count rather than consuming one of its slots, so asking to
    keep one snapshot never removes the one clients are pinning.

    An absent pointer means no snapshot is pinned and retention proceeds. A
    pointer that exists but does not parse is different: the pin is unknown, so
    no snapshot is removed at all. Guessing here would delete the pinned
    snapshot exactly when the file naming it is already damaged.
    """
    snapshots = list_snapshots(channel_root)
    pinned = pinned_snapshot(channel_root)
    pin_unknown = pinned is None and pointer_present(channel_root)

    if pin_unknown:
        removed: list[str] = []
    else:
        by_age_kept = set(snapshots[-keep:] if keep > 0 else [])
        removed = [s for s in snapshots if s not in by_age_kept and s != pinned]

    kept = [s for s in snapshots if s not in set(removed)]
    doomed = [channel_root / _SNAPSHOTS_DIR / snap for snap in removed]

    # Sweep only inside the channel the caller named; scan for references across
    # the whole feed. See the module docstring on why the two scopes differ.
    stale, unreadable_indexes = stale_metadata(channel_root, skip=doomed)
    scan = scan_live_metadata(feed_root, skip=doomed)

    # A repository whose package list could not be read still serves that list,
    # so nothing it might reference may be reclaimed. Refusing the whole pool
    # sweep is the only safe reading: which entries that repository would have
    # named is precisely what could not be determined.
    orphans = [entry for entry in pool_entries(channel_root) if entry not in scan.pool_paths] if scan.complete else []

    return RetentionPlan(
        channel_root=channel_root,
        kept_snapshots=kept,
        removed_snapshots=removed,
        pinned=pinned,
        stale_metadata=stale,
        orphan_pool=orphans,
        unreadable_indexes=sorted(set(unreadable_indexes) | set(scan.unreadable_indexes)),
        unreadable_primaries=scan.unreadable_primaries,
        foreign_snapshot_entries=foreign_snapshot_entries(channel_root),
    )


def apply_retention(plan: RetentionPlan, *, feed_root: Path, confirm: bool = False) -> RetentionResult:
    """Carry out the plan, but only when asked.

    Preview is the default for the same reason it is in
    :mod:`bakar.feed_reclaim`: a retained snapshot costs disk, a wrongly removed
    pool entry costs a rebuild.

    Every removal is reported only after it succeeded, and freed bytes are
    counted the same way - a snapshot tree that could not be removed appears in
    ``failed_snapshots``, not in ``removed_snapshots``.

    On a real run the removals are audited afterwards by re-reading every
    retained repository's live metadata and checking each reference still
    resolves. The audit deliberately re-reads from disk instead of reusing the
    plan's reference set: sharing it would make the audit agree with the plan by
    construction, and what it exists to catch is a mistake in the plan's own
    ordering.
    """
    if not confirm:
        return RetentionResult(
            applied=False,
            removed_snapshots=list(plan.removed_snapshots),
            removed_metadata=list(plan.stale_metadata),
            removed_pool=list(plan.orphan_pool),
            freed_bytes=plan.reclaimable_bytes,
        )

    freed = 0

    removed_snapshots: list[str] = []
    failed_snapshots: list[str] = []
    for snap in plan.removed_snapshots:
        target = plan.channel_root / _SNAPSHOTS_DIR / snap
        size = _tree_size(target)
        if _rmtree(target):
            removed_snapshots.append(snap)
            freed += size
        else:
            failed_snapshots.append(snap)

    removed_metadata: list[Path] = []
    for path in plan.stale_metadata:
        size = _size(path)
        if _unlink(path):
            removed_metadata.append(path)
            freed += size

    removed_pool: list[Path] = []
    for path in plan.orphan_pool:
        size = _size(path)
        if _unlink(path):
            removed_pool.append(path)
            freed += size

    audit_dangling, audit_unreadable = audit_references(feed_root)

    return RetentionResult(
        applied=True,
        removed_snapshots=removed_snapshots,
        removed_metadata=removed_metadata,
        removed_pool=removed_pool,
        failed_snapshots=failed_snapshots,
        dangling=audit_dangling,
        unreadable_primaries=audit_unreadable,
        freed_bytes=freed,
    )


def audit_references(root: Path) -> tuple[list[tuple[Path, Path]], list[Path]]:
    """Return (dangling references, primaries that could not be read).

    The check a retention run has to pass. An empty dangling list is acceptable
    only alongside an empty unreadable list: a repository whose metadata names a
    package that is not there serves a resolvable index and then fails at
    download, and a repository whose package list cannot be read is one this
    audit simply did not check. Reporting the second as clean is how the first
    escapes notice.
    """
    dangling: list[tuple[Path, Path]] = []
    unreadable: list[Path] = []

    for repodata in _repodata_dirs(root):
        live = live_metadata(repodata)
        if live is None:
            continue
        index = repodata / _INDEX
        dangling.extend((index, path) for path in sorted(live) if not path.exists())
        repo_root = repodata.parent
        for primary in sorted(p for p in live if "primary" in p.name):
            body = _read_primary(primary)
            if body is None:
                unreadable.append(primary)
                continue
            for href in _LOCATION_HREF.findall(body):
                target = repo_root / href
                if not target.exists():
                    dangling.append((index, target))

    return dangling, unreadable


def _resolves_inside(path: Path, parent: Path, *, unknown: bool) -> bool:
    """True when ``path`` resolves inside ``parent``, symlinks included.

    ``unknown`` is the answer for a path that cannot be resolved, and it is a
    required argument on purpose. The safe direction differs by caller - a
    reclaim gate wants "assume inside, do not delete", a skip list wants "assume
    outside, keep scanning" - and the two are opposite. An implicit default here
    is how one caller silently inherits the other's polarity;
    :mod:`bakar.feed_reclaim` has its own copy of this and defaults the other
    way, which is exactly the trap.
    """
    try:
        return path.resolve().is_relative_to(parent.resolve())
    except OSError:
        return unknown


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _tree_size(root: Path) -> int:
    if not root.is_dir() or root.is_symlink():
        return 0
    total = 0
    for entry in root.rglob("*"):
        if entry.is_file() and not entry.is_symlink():
            total += _size(entry)
    return total


def _unlink(path: Path) -> bool:
    try:
        path.unlink()
    except OSError:
        return False
    return True


def _rmtree(root: Path) -> bool:
    """Remove a snapshot tree, returning whether it is gone.

    Refuses a symlinked root outright. Walking into it would delete the contents
    of whatever it points at - an archive directory, or another release's tree -
    while leaving the link itself behind. Guarding only the nested entries, as an
    earlier version did, misses the one case that reaches outside the feed.
    """
    if root.is_symlink() or not root.is_dir():
        return False
    for entry in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if entry.is_dir() and not entry.is_symlink():
            with suppress(OSError):
                entry.rmdir()
        else:
            _unlink(entry)
    with suppress(OSError):
        root.rmdir()
    return not root.exists()
