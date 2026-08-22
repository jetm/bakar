"""Publish a build's software inventory alongside the packages it describes.

An inventory is a statement about an exact set of packages, so it needs an
identifier that cannot drift away from them. The head of a feed is mutable by
definition, so publishing there would let the two disagree silently - the
inventory would keep describing packages the head has since replaced. Everything
here therefore publishes under the immutable snapshot and nowhere else.

That is also the answer to "what stable identifier does a published inventory
carry", which is otherwise awkward for this tree: ``avocado-image-rootfs.bb``
clobbers ``IMAGE_NAME``, ``IMAGE_LINK_NAME`` and ``IMAGE_VERSION_SUFFIX`` as
deliberate tree-wide policy, so a build-time version cannot be reintroduced
without moving every image artifact's filename. Assigning the identifier at
publish time sidesteps that entirely, and the snapshot id already exists.

A consumer wanting "the current inventory" reads ``snapshots-latest.json`` for
the snapshot id and derives the path. That is one mutable pointer rather than
two, so there is no second copy to fall out of step with the first.

WHICH document. The per-image SPDX document, not the per-package set. Measured
on a real build: the image document is 7.6 MB with one ``software_Sbom`` root
covering the whole rootfs, while the per-package set is 459 MB across 20,609
files, most of which are recipe and staging plumbing rather than anything a
consumer of the feed would install. Publishing the set would also advertise
native and cross recipes that ship no package.

Most builds publish nothing here, and that is expected rather than a failure -
only a build with SPDX enabled emits an inventory at all.
"""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

# Where inventories sit inside a snapshot, as a sibling of the repository
# subpaths rather than inside one: the document describes the image, which spans
# several repositories, so filing it under any single one would misattribute it.
SBOM_DIR = "sbom"

# A build writes several JSON files per image - a manifest, testdata, a stone
# descriptor - so the inventory is identified by the compound suffix. Matching
# "*.json" would publish four files and call three of them inventories.
_SBOM_GLOB = "*.spdx.json"

_SNAPSHOTS_DIR = "snapshots"


def find_image_sboms(deploy_root: Path) -> list[Path]:
    """Return the image inventories a build emitted, sorted by path.

    Empty when the build had SPDX disabled or wrote no images directory, which
    is the common case rather than an error.
    """
    images = deploy_root / "images"
    if not images.is_dir():
        return []
    return sorted(images.glob(f"*/{_SBOM_GLOB}"))


def publish_sbom(channel_root: Path, snapshot: str, sboms: list[Path]) -> list[Path]:
    """Copy each inventory under ``snapshots/<snapshot>/sbom/`` and return the paths.

    Copied verbatim. A consumer codes against exactly what the build emitted, so
    reserialising would change the document's checksum and make the published
    artifact a different one from the build attested to.

    Publishes nothing at all - not even the directory - when given no
    inventories: an empty ``sbom/`` would assert that an inventory exists and is
    empty, which is a different and false claim from having published none.
    """
    if not sboms:
        return []

    target = channel_root / _SNAPSHOTS_DIR / snapshot / SBOM_DIR
    target.mkdir(parents=True, exist_ok=True)

    published = []
    for sbom in sboms:
        destination = target / sbom.name
        shutil.copy2(sbom, destination)
        published.append(destination)
    return published
