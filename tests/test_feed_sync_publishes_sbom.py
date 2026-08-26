"""Publishing an inventory into the snapshot a client will pin.

``feed_sbom.publish_sbom`` has existed, tested, with no production caller since
the feed landed - deliberately, because the only document available to publish
carried vulnerability data. Now that ``--sbom`` produces a filtered one, this
wires it in.

**Where it happens is the whole point.** ``sync_paths`` renders every repository
and writes the pointer LAST, so an interrupted sync leaves the previous pointer
rather than announcing a snapshot whose contents are missing. An inventory
published after the pointer reintroduces exactly that hazard one layer down: a
consumer reads ``snapshots-latest.json``, derives ``snapshots/<id>/sbom/``, and
gets a 404 for a window whose length is nobody's decision. So publication goes
inside the sync, before the pointer write, and the ordering test below is what
holds it there.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bakar import feed as feed_mod
from bakar.feed_sbom import SBOM_DIR

pytestmark = pytest.mark.unit


@pytest.fixture
def tree(tmp_path: Path) -> dict[str, Path]:
    """A deploy tree with one repo declared, plus a filtered inventory."""
    deploy = tmp_path / "deploy" / "rpm"
    deploy.mkdir(parents=True)
    (deploy.parent / "rpm" / "avocado-repo.map").write_text("target/qemux86-64\n", encoding="utf-8")

    sbom = tmp_path / "filtered" / "avocado-qemux86-64"
    sbom.mkdir(parents=True)
    doc = sbom / "avocado-image-rootfs-qemux86-64.spdx.json"
    doc.write_text(json.dumps({"@graph": [{"type": "software_Package"}]}), encoding="utf-8")

    return {
        "deploy": deploy,
        "feed": tmp_path / "_feed",
        "stage": tmp_path / "_feed-stage",
        "doc": doc,
    }


@pytest.fixture
def order(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record publication and pointer-write order without a real renderer."""
    seen: list[str] = []

    real_publish = feed_mod.feed_sbom.publish_sbom
    real_pointer = feed_mod.write_latest_pointer

    def publish(channel_root, snapshot, sboms):
        seen.append("publish")
        return real_publish(channel_root, snapshot, sboms)

    def pointer(channel_root, snapshot, *, machines):
        seen.append("pointer")
        return real_pointer(channel_root, snapshot, machines=machines)

    monkeypatch.setattr(feed_mod.feed_sbom, "publish_sbom", publish)
    monkeypatch.setattr(feed_mod, "write_latest_pointer", pointer)
    monkeypatch.setattr(feed_mod, "stage_build", lambda **_kw: Path("/staged"))
    monkeypatch.setattr(feed_mod, "render_repo", lambda **_kw: None)
    # A declared repo renders only when its staged directory exists.
    monkeypatch.setattr(Path, "is_dir", lambda self: True)
    return seen


def _sync(tree, sboms):
    return feed_mod.sync_paths(
        feed_root=tree["feed"],
        stage_root=tree["stage"],
        deploy_dir=tree["deploy"],
        scripts=Path("/scripts"),
        release="dev",
        channel="local",
        snapshot="20260101T000000Z",
        sboms=sboms,
    )


def test_inventory_is_published_before_the_pointer(tree, order) -> None:
    """The ordering this whole module exists to hold.

    Reversed, a client that follows a fresh pointer to the inventory it names
    gets a 404 - and unlike a missing repository, nothing else in the sync would
    report it.
    """
    _sync(tree, [tree["doc"]])

    assert order == ["publish", "pointer"]


def test_the_inventory_lands_under_the_snapshot(tree, order) -> None:
    result = _sync(tree, [tree["doc"]])

    published = tree["feed"] / "dev" / "local" / "snapshots" / "20260101T000000Z" / SBOM_DIR
    assert (published / "avocado-image-rootfs-qemux86-64.spdx.json").is_file()
    assert result["sboms"] == [published / "avocado-image-rootfs-qemux86-64.spdx.json"]


def test_a_sync_with_no_inventory_writes_no_sbom_directory(tree, order) -> None:
    """An empty ``sbom/`` asserts that an inventory exists and is empty, which is
    a different and false claim from having published none. Most builds publish
    nothing here and that is the ordinary case, not a failure.
    """
    result = _sync(tree, None)

    snapshot_root = tree["feed"] / "dev" / "local" / "snapshots" / "20260101T000000Z"
    assert not (snapshot_root / SBOM_DIR).exists()
    assert result["sboms"] == []


def test_the_pointer_is_still_written_when_nothing_is_published(tree, order) -> None:
    """Publishing no inventory must not suppress the pin - the packages are the
    reason the snapshot exists and the inventory is an addition to it.
    """
    _sync(tree, None)

    assert order[-1] == "pointer"
