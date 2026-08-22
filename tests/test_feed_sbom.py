"""Tests for publishing a build's software inventory under its snapshot id.

Publishing under the snapshot, and only under the snapshot, is the point. The
inventory describes an exact set of packages, so it needs an identifier that
cannot drift away from them - and the head is mutable by definition. The
existing ``snapshots-latest.json`` already names the current snapshot, so a
consumer wanting "the current inventory" derives the path from that rather than
needing a second mutable copy that can disagree with the first.

The absent-inventory path is the common one rather than an edge case: of the
build trees on this host only one has SPDX enabled, so most syncs publish no
inventory at all. That has to be a report, never a failure and never an empty
document.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from bakar.feed_sbom import SBOM_DIR, find_image_sboms, publish_sbom

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


def _images(tmp_path: Path, machine: str = "avocado-qemux86-64") -> Path:
    """A deploy root with an images dir mirroring a real build's file mix."""
    images = tmp_path / "deploy" / "images" / machine
    images.mkdir(parents=True)
    # Only the .spdx.json is an inventory. The siblings are real files a build
    # emits beside it and are the reason the match cannot simply be "*.json".
    (images / "avocado-image-rootfs-qemux86-64.spdx.json").write_text('{"@graph": []}')
    (images / "avocado-image-rootfs-qemux86-64.json").write_text("{}")
    (images / "avocado-image-rootfs-qemux86-64.testdata.json").write_text("{}")
    (images / "avocado-image-initramfs-qemux86-64.json").write_text("{}")
    (images / "stone-qemux86-64.json").write_text("{}")
    return tmp_path / "deploy"


def test_find_image_sboms_picks_only_the_spdx_document(tmp_path) -> None:
    """The inventory is the ``.spdx.json``, not its same-named siblings.

    A build writes several JSON files per image - testdata, the image manifest,
    a stone descriptor - so matching ``*.json`` would publish four files that
    are not inventories and would misrepresent three of them as one.
    """
    deploy = _images(tmp_path)

    found = find_image_sboms(deploy)

    assert [p.name for p in found] == ["avocado-image-rootfs-qemux86-64.spdx.json"]


def test_find_image_sboms_is_empty_when_the_build_emitted_none(tmp_path) -> None:
    """A build without SPDX enabled yields nothing, not an error."""
    (tmp_path / "deploy" / "images" / "avocado-qemuarm64").mkdir(parents=True)

    assert find_image_sboms(tmp_path / "deploy") == []


def test_find_image_sboms_is_empty_with_no_images_directory(tmp_path) -> None:
    """A deploy root with no images dir at all is also just empty."""
    (tmp_path / "deploy").mkdir()

    assert find_image_sboms(tmp_path / "deploy") == []


def test_publish_sbom_writes_under_the_snapshot_id(tmp_path) -> None:
    """The inventory lands under ``snapshots/<id>/`` beside the repositories."""
    deploy = _images(tmp_path)
    channel = tmp_path / "feed" / "2026" / "edge"

    published = publish_sbom(channel, "20260822T090501Z", find_image_sboms(deploy))

    expected = channel / "snapshots" / "20260822T090501Z" / SBOM_DIR / "avocado-image-rootfs-qemux86-64.spdx.json"
    assert published == [expected]
    assert expected.is_file()


def test_publish_sbom_copies_the_document_verbatim(tmp_path) -> None:
    """Published bytes equal source bytes.

    A consumer codes against exactly what the build emitted. Reserialising or
    reformatting would change checksums and make the published document a
    different artifact from the one the build attested to.
    """
    deploy = _images(tmp_path)
    source = find_image_sboms(deploy)[0]
    channel = tmp_path / "feed" / "2026" / "edge"

    published = publish_sbom(channel, "SNAP", [source])

    assert published[0].read_bytes() == source.read_bytes()


def test_publish_sbom_creates_nothing_when_there_is_no_inventory(tmp_path) -> None:
    """No inventory means no directory, so absence is not mistaken for empty.

    Creating an empty ``sbom/`` would tell a consumer an inventory was published
    and is empty, which is a different and false claim from publishing none.
    """
    channel = tmp_path / "feed" / "2026" / "edge"

    published = publish_sbom(channel, "SNAP", [])

    assert published == []
    assert not (channel / "snapshots" / "SNAP" / SBOM_DIR).exists()


def test_publish_sbom_is_idempotent(tmp_path) -> None:
    """Re-publishing the same snapshot rewrites the same path with the same bytes.

    A sync can be re-run after a partial failure, so publishing twice must not
    accumulate variants of one snapshot's inventory.
    """
    deploy = _images(tmp_path)
    sboms = find_image_sboms(deploy)
    channel = tmp_path / "feed" / "2026" / "edge"

    first = publish_sbom(channel, "SNAP", sboms)
    second = publish_sbom(channel, "SNAP", sboms)

    assert first == second
    sbom_dir = channel / "snapshots" / "SNAP" / SBOM_DIR
    assert len(list(sbom_dir.iterdir())) == 1


def test_publish_sbom_publishes_every_inventory_it_is_given(tmp_path) -> None:
    """A build emitting more than one image inventory publishes all of them."""
    deploy = _images(tmp_path)
    extra = deploy / "images" / "avocado-qemux86-64" / "avocado-image-dev-qemux86-64.spdx.json"
    extra.write_text('{"@graph": []}')
    channel = tmp_path / "feed" / "2026" / "edge"

    published = publish_sbom(channel, "SNAP", find_image_sboms(deploy))

    assert sorted(p.name for p in published) == [
        "avocado-image-dev-qemux86-64.spdx.json",
        "avocado-image-rootfs-qemux86-64.spdx.json",
    ]


def test_publish_sbom_does_not_write_to_the_mutable_head(tmp_path) -> None:
    """Nothing is published outside the snapshot directory.

    The head moves; an inventory pinned to it would drift away from the packages
    it describes. The current inventory is found through snapshots-latest.json.
    """
    deploy = _images(tmp_path)
    channel = tmp_path / "feed" / "2026" / "edge"

    publish_sbom(channel, "SNAP", find_image_sboms(deploy))

    outside = [p for p in channel.rglob("*.spdx.json") if "snapshots/SNAP" not in str(p)]
    assert outside == []
