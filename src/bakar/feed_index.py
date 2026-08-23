"""Derive the target index from the rendered feed.

Read from the tree, never from the build-time map fragments the historical
method assembled. A map declares what a machine COULD publish; the tree records
what it did. Those differ in practice - a root declared but never staged has no
metadata - and advertising the declared set tells a client to fetch a repository
that is not there.

The production renderer derives its index the same way, by listing what it
rendered, and its own docstring calls the fragment-based derivation historical.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_INDEX = "repomd.xml"
_TARGETS = "targets.json"

# Extension repositories belong TO a machine and are not machines themselves.
# Reading `target/<machine>-ext` as a target would advertise it and then emit
# `target/<machine>-ext-ext` in its canonical list.
_EXT_SUFFIX = "-ext"


def derive_targets(channel_root: Path) -> Iterator[str]:
    """Yield the machines the channel has actually rendered, sorted.

    A machine counts when its target repository carries a metadata index.
    Sorted so re-deriving an unchanged feed produces identical bytes - the index
    is rewritten after every sync, and churn here would mask real changes.

    One level of ``iterdir``, never a recursive walk. Snapshots repeat the whole
    repository layout beneath themselves, so a walk would advertise every machine
    once per retained snapshot; staying flat under ``target/`` is what prevents
    that, rather than any filter on the snapshots directory name.
    """
    target_dir = channel_root / "target"
    if not target_dir.is_dir():
        return
    for entry in sorted(target_dir.iterdir()):
        if not entry.is_dir() or entry.name.endswith(_EXT_SUFFIX):
            continue
        if (entry / "repodata" / _INDEX).is_file():
            yield entry.name


def canonical_repos(machine: str) -> list[str]:
    """Return the four repositories that lock a target, in production's order.

    The release-global toolchain repository first because it is shared across
    machines; the extension repository last, and listed even when empty, because
    an empty extension repository is valid and its content arrives from
    ``avocado ext package`` rather than from a build.
    """
    return [
        "sdk/all",
        f"target/{machine}",
        f"sdk/{machine}",
        f"target/{machine}{_EXT_SUFFIX}",
    ]


def write_targets_index(channel_root: Path) -> Path:
    """Write ``targets.json`` for the channel and return its path.

    An empty feed yields an empty mapping rather than no file, so a client
    fetching the index gets a valid answer instead of a 404 it has to interpret.
    """
    targets = {machine: canonical_repos(machine) for machine in derive_targets(channel_root)}
    channel_root.mkdir(parents=True, exist_ok=True)
    path = channel_root / _TARGETS
    path.write_text(json.dumps(targets, indent=2, sort_keys=True) + "\n")
    return path
