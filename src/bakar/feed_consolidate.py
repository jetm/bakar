"""Discover build trees worth consolidating, and size what consolidating saves.

Discovery is anchored on a build DECLARING its packages, never on finding files
that look like packages. The distinction is not pedantic: dnf vendors its own
test fixtures inside every build's work tree - thousands of files named like
``kernel-doc-4.11.0-1.noarch.rpm`` at a few kilobytes each - and a scan that
collects ``*.rpm`` puts those in a served repository as installable packages
nobody built. An ``avocado-repo.map`` is the build's own statement of what it
produced and which repository each part belongs to, so that is the anchor.

Identity is the resolved path, because a tree is routinely reachable by more
than one. On this host ``repos/work/peridio-scarthgap-build`` is a symlink to the
shared workspace, so three of four build trees appear twice under a naive walk.
Reporting them as duplicates would promise reclaimable space that does not
exist.

Sizing counts distinct CONTENT rather than paths, for the same reason in a
different guise. Two trees really can hold byte-identical packages, and that
overlap is genuinely reclaimable; a hardlink between one tree's own directories
is not, and OpenEmbedded makes those by the thousand when it hardlinks
``oe-rootfs-repo`` against ``tmp/deploy/rpm``. Measured across this host once:
203 GB of apparent RPM bytes over 123 GB of real ones.

Release and channel come from the build's declared ``DISTRO_CODENAME`` and from
nowhere else. The map carries a ``$releasever`` placeholder that the renderer
substitutes, so a tree's release cannot be recovered from it, and a release
guessed from a directory name files packages where no target will resolve them.
A tree that declares nothing reports None - the caller decides what to do with
an unknown, which is a decision and not a default. Worth knowing when reading
that None: every local build here declares ``dev/local``, so the ``2024/edge``
and ``2026/edge`` paths in existing staged feeds came from whoever ran the
staging script, not from any build.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from bakar.feed import parse_repo_map, sync_paths

if TYPE_CHECKING:
    from pathlib import Path

_MAP_NAME = "avocado-repo.map"

# Bitbake writes the resolved value of many variables here, which makes it the
# one place a finished tree still states the release it was built for.
_TESTDATA_GLOB = "images/*/*.testdata.json"

_CODENAME_KEY = "DISTRO_CODENAME"
_MACHINE_KEY = "MACHINE"


@dataclass(frozen=True)
class DiscoveredTree:
    """One build tree offered as a consolidation source."""

    deploy_dir: Path
    """The RPM deploy directory holding the map."""

    deploy_root: Path
    """Its parent, where images and their inventories live."""

    repo_roots: list[str] = field(default_factory=list)
    machine: str | None = None
    release: str | None = None
    channel: str | None = None


def _read_testdata(deploy_root: Path) -> dict[str, str]:
    """Return the first readable testdata mapping, or an empty one."""
    for candidate in sorted(deploy_root.glob(_TESTDATA_GLOB)):
        try:
            loaded = json.loads(candidate.read_text())
        except OSError, ValueError:
            continue
        if isinstance(loaded, dict):
            return loaded
    return {}


def _split_codename(codename: str | None) -> tuple[str | None, str | None]:
    """Split ``release/channel``, returning (None, None) when unusable.

    A codename without a separator is not half an answer - it names a release
    with no channel, and inventing ``edge`` for it would be the guess this
    module exists to avoid.
    """
    if not codename or "/" not in codename:
        return None, None
    release, channel = codename.split("/", 1)
    return (release or None), (channel or None)


def discover_trees(search_roots: list[Path]) -> list[DiscoveredTree]:
    """Return every distinct build tree under ``search_roots``, in path order.

    A tree reachable by several paths is returned once, keyed on its resolved
    deploy directory.
    """
    seen: set[Path] = set()
    trees: list[DiscoveredTree] = []

    for root in search_roots:
        for map_path in sorted(root.rglob(_MAP_NAME)):
            deploy_dir = map_path.parent.resolve()
            if deploy_dir in seen:
                continue
            seen.add(deploy_dir)

            deploy_root = deploy_dir.parent
            testdata = _read_testdata(deploy_root)
            release, channel = _split_codename(testdata.get(_CODENAME_KEY))
            trees.append(
                DiscoveredTree(
                    deploy_dir=deploy_dir,
                    deploy_root=deploy_root,
                    repo_roots=parse_repo_map(map_path),
                    machine=testdata.get(_MACHINE_KEY),
                    release=release,
                    channel=channel,
                )
            )
    return trees


def consolidate(  # noqa: PLR0913 - the roots, the scripts and an optional release override are each independent
    trees: list[DiscoveredTree],
    *,
    feed_root: Path,
    stage_root: Path,
    scripts: Path,
    release: str | None = None,
    channel: str | None = None,
    snapshot: str | None = None,
) -> list[dict[str, object]]:
    """Sync every discovered tree into one feed, returning a result per tree.

    Each tree is filed under the release and channel IT declared, so two trees
    from different releases cannot land on top of each other. An explicit
    ``release``/``channel`` overrides that for every tree, which is how an
    operator consolidates local builds - they all declare ``dev/local`` - into
    the channel a client is configured to fetch.

    A tree that declared nothing and got no override is SKIPPED with a reason,
    never filed under a guess. Choosing a plausible release for it would put its
    packages at a path no target resolves, which reads as a successful
    consolidation and serves nothing.

    Trees are synced in order and each is independent, so a failure part-way
    leaves the trees already synced intact and correctly announced - their
    pointer writes have completed. The exception propagates rather than being
    collected, because a half-consolidated feed the caller believes is whole is
    worse than one it knows stopped.
    """
    results: list[dict[str, object]] = []

    for tree in trees:
        tree_release = release or tree.release
        tree_channel = channel or tree.channel
        if not tree_release or not tree_channel:
            results.append(
                {
                    "tree": tree.deploy_dir,
                    "machine": tree.machine,
                    "skipped": (
                        "no release/channel declared by the build and none supplied; "
                        "filing it under a guessed release would publish packages "
                        "where no target resolves them"
                    ),
                }
            )
            continue

        outcome = sync_paths(
            feed_root=feed_root,
            stage_root=stage_root,
            deploy_dir=tree.deploy_dir,
            scripts=scripts,
            release=tree_release,
            channel=tree_channel,
            snapshot=snapshot,
        )
        results.append({"tree": tree.deploy_dir, "machine": tree.machine, **outcome})

    return results


def consolidation_savings(trees: list[DiscoveredTree]) -> dict[str, int]:
    """Size what consolidating ``trees`` would hold, counting content once.

    ``rpm_paths`` counts package files a reclaim pass would encounter, so a
    hardlink counts - it is a real second path. ``distinct_contents`` counts the
    bytes behind them, which is what a content-addressed pool would store, so
    the difference between the two is the overlap.

    Two signals collapse a path, and they cover different cases. Sharing an
    inode is proof of identical bytes and catches OpenEmbedded hardlinking
    ``oe-rootfs-repo`` against the deploy directory, which it does by the
    thousand. Matching size and name is a strong hint for the same package
    landing in two separate trees, where the inodes necessarily differ.

    Neither is a hash, deliberately. This is a report over tens of thousands of
    files, not the reclaim gate; the gate keys on the pool's own sha256 and is
    the only thing permitted to authorise a deletion. Being approximate here
    cannot cost anything but an imprecise figure.
    """
    paths = 0
    seen_inodes: set[tuple[int, int]] = set()
    contents: set[str] = set()

    for tree in trees:
        for rpm in sorted(tree.deploy_dir.rglob("*.rpm")):
            paths += 1
            stat = rpm.stat()
            inode = (stat.st_dev, stat.st_ino)
            if inode in seen_inodes:
                continue
            seen_inodes.add(inode)
            contents.add(f"{stat.st_size}:{rpm.name}")

    return {
        "trees": len(trees),
        "rpm_paths": paths,
        "distinct_contents": len(contents),
    }
