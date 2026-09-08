"""Tests for the gate that decides whether a source package may be deleted.

Everything here is about refusing. Deleting a package that the feed cannot hand
back costs a multi-hour rebuild, and the asymmetry is total: a retained
duplicate costs disk, which is cheap and reversible.

The gate is two conditions, not one. Content present in the pool is not enough,
because pooled content that no repository's metadata references cannot be served
- the feed holds the bytes and no client can reach them. Both must hold.

Three refusals are structural rather than conditional, and each has a way of
looking safe. An unreadable pool is not an empty pool, so it authorises nothing.
The feed's own content is never a source, however the search roots are phrased.
And a path under an NFS export can be a tree another node is building in right
now, which no local check can see.
"""

from __future__ import annotations

import gzip
import hashlib
import os
from typing import TYPE_CHECKING

import pytest

from bakar.feed_reclaim import plan_reclaim, pool_shas, referenced_shas

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _pool(channel: Path, data: bytes) -> str:
    """Put ``data`` in the pool the way the renderer does, returning its sha."""
    digest = _sha(data)
    entry = channel / "_pkgs" / digest[:2] / f"{digest}.rpm"
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_bytes(data)
    return digest


def _repo(channel: Path, subpath: str, shas: list[str], *, stale: list[str] | None = None) -> None:
    """Render a repository whose primary references ``shas``.

    When ``stale`` is given, a second primary naming those shas is left in the
    directory but NOT named by repomd.xml - mirroring the renderer, which leaves
    the previous primary behind on every re-render.
    """
    repodata = channel / subpath / "repodata"
    repodata.mkdir(parents=True, exist_ok=True)
    depth = len(subpath.strip("/").split("/"))
    up = "../" * depth

    def write_primary(entries: list[str], name: str) -> None:
        body = "".join(f'<package><location href="{up}_pkgs/{s[:2]}/{s}.rpm"/></package>' for s in entries)
        with gzip.open(repodata / name, "wb") as fh:
            fh.write(f"<metadata>{body}</metadata>".encode())

    if stale:
        write_primary(stale, "stale-primary.xml.gz")
    write_primary(shas, "live-primary.xml.gz")
    (repodata / "repomd.xml").write_text(
        '<repomd><data type="primary"><location href="repodata/live-primary.xml.gz"/></data></repomd>'
    )


def _source(root: Path, name: str, data: bytes) -> Path:
    """A source package in a build tree's deploy directory."""
    rpm = root / "build" / "tmp" / "deploy" / "rpm" / "core2_64"
    rpm.mkdir(parents=True, exist_ok=True)
    path = rpm / name
    path.write_bytes(data)
    return path


def test_pool_shas_reads_the_pool_by_content(tmp_path) -> None:
    """The pool index is the set of sha256 names the renderer wrote."""
    digest = _pool(tmp_path, b"pkg")

    assert pool_shas(tmp_path) == {digest}


def test_pool_shas_is_none_when_the_pool_cannot_be_read(tmp_path) -> None:
    """An absent pool reports None, distinct from an empty one.

    None and empty must not collapse: an empty pool means nothing is pooled, an
    unreadable one means nothing is known, and only the first can ever support a
    deletion decision.
    """
    assert pool_shas(tmp_path / "nonexistent") is None


def test_referenced_shas_follows_repomd_not_a_glob(tmp_path) -> None:
    """Only the primary that repomd.xml names counts as live.

    The renderer leaves the previous primary in place on every re-render, so a
    directory routinely holds several. A glob can return a stale one that still
    parses, and a sha referenced only there would be treated as reachable when
    no client can reach it.
    """
    live, dead = _sha(b"live"), _sha(b"dead")
    _repo(tmp_path, "target/qemux86-64", [live], stale=[dead])

    assert referenced_shas(tmp_path) == {live}


def test_a_pooled_and_referenced_package_is_eligible(tmp_path) -> None:
    """Both conditions met: the feed can hand this back, so the source may go."""
    channel = tmp_path / "feed" / "dev" / "local"
    data = b"pkg-a"
    digest = _pool(channel, data)
    _repo(channel, "target/qemux86-64", [digest])
    source = _source(tmp_path / "tree", "pkg-a-1.0.rpm", data)

    plan = plan_reclaim([source], channel_root=channel, feed_root=tmp_path / "feed")

    assert [c.path for c in plan.eligible] == [source]


def test_a_pooled_but_unreferenced_package_is_retained(tmp_path) -> None:
    """Pooled is not enough: unreferenced content cannot be served."""
    channel = tmp_path / "feed" / "dev" / "local"
    data = b"orphan"
    _pool(channel, data)
    _repo(channel, "target/qemux86-64", [_sha(b"something-else")])
    source = _source(tmp_path / "tree", "orphan-1.0.rpm", data)

    plan = plan_reclaim([source], channel_root=channel, feed_root=tmp_path / "feed")

    assert plan.eligible == []
    assert plan.retained[0].reason is not None
    assert "no repository" in plan.retained[0].reason


def test_a_package_absent_from_the_pool_is_retained(tmp_path) -> None:
    """Never pooled means the feed does not have it at all."""
    channel = tmp_path / "feed" / "dev" / "local"
    _pool(channel, b"unrelated")
    _repo(channel, "target/qemux86-64", [_sha(b"unrelated")])
    source = _source(tmp_path / "tree", "missing-1.0.rpm", b"never-pooled")

    plan = plan_reclaim([source], channel_root=channel, feed_root=tmp_path / "feed")

    assert plan.eligible == []
    assert plan.retained[0].reason is not None
    assert "not in the pool" in plan.retained[0].reason


def test_an_unreadable_pool_makes_nothing_eligible(tmp_path) -> None:
    """A pool that cannot be read authorises no deletion whatsoever.

    An unanswerable check is not a passing check, and this is the one failure
    mode that would otherwise mark everything eligible - an empty pool index
    trivially contains nothing, so every source would look absent rather than
    unknown.
    """
    channel = tmp_path / "feed" / "dev" / "local"
    channel.mkdir(parents=True)
    source = _source(tmp_path / "tree", "pkg-1.0.rpm", b"data")

    plan = plan_reclaim([source], channel_root=channel, feed_root=tmp_path / "feed")

    assert plan.eligible == []
    assert plan.pool_readable is False


def test_the_gate_keys_on_content_not_on_file_name(tmp_path) -> None:
    """Two sources sharing a name are judged by their bytes.

    A name-keyed gate deletes the variant that was never pooled because its
    namesake was, which is exactly how a package is lost while the check
    reports success.
    """
    channel = tmp_path / "feed" / "dev" / "local"
    pooled_bytes, other_bytes = b"variant-A", b"variant-B"
    digest = _pool(channel, pooled_bytes)
    _repo(channel, "target/qemux86-64", [digest])
    same_name = "tzdata-2026c-r0.noarch.rpm"
    a = _source(tmp_path / "tree-a", same_name, pooled_bytes)
    b = _source(tmp_path / "tree-b", same_name, other_bytes)

    plan = plan_reclaim([a, b], channel_root=channel, feed_root=tmp_path / "feed")

    assert [c.path for c in plan.eligible] == [a]
    assert [c.path for c in plan.retained] == [b]


def test_the_feeds_own_pool_is_never_a_source(tmp_path) -> None:
    """Content inside the feed root is never eligible, whatever is passed in."""
    channel = tmp_path / "feed" / "dev" / "local"
    data = b"pkg"
    digest = _pool(channel, data)
    _repo(channel, "target/qemux86-64", [digest])
    pool_entry = channel / "_pkgs" / digest[:2] / f"{digest}.rpm"

    plan = plan_reclaim([pool_entry], channel_root=channel, feed_root=tmp_path / "feed")

    assert plan.eligible == []
    assert plan.retained[0].reason is not None
    assert "inside the feed" in plan.retained[0].reason


def test_the_feed_reached_through_a_symlink_is_still_protected(tmp_path) -> None:
    """Containment is tested on resolved paths, not on the strings given.

    A search root holding a symlink to the feed is how the feed gets offered as
    its own source, and a string comparison misses it completely.
    """
    channel = tmp_path / "feed" / "dev" / "local"
    data = b"pkg"
    digest = _pool(channel, data)
    _repo(channel, "target/qemux86-64", [digest])
    alias = tmp_path / "alias"
    os.symlink(tmp_path / "feed", alias)
    via_alias = alias / "dev" / "local" / "_pkgs" / digest[:2] / f"{digest}.rpm"

    plan = plan_reclaim([via_alias], channel_root=channel, feed_root=tmp_path / "feed")

    assert plan.eligible == []


def test_an_exported_path_is_not_eligible_without_an_override(tmp_path) -> None:
    """A path under an NFS export needs the operator to name it.

    Another node can be building in that tree right now, and no check available
    here can see it. The shared workspace on this host is exported read-write to
    both cluster nodes and holds three of the four build trees.
    """
    channel = tmp_path / "feed" / "dev" / "local"
    data = b"pkg"
    digest = _pool(channel, data)
    _repo(channel, "target/qemux86-64", [digest])
    tree = tmp_path / "shared-workspace"
    source = _source(tree, "pkg-1.0.rpm", data)

    plan = plan_reclaim(
        [source],
        channel_root=channel,
        feed_root=tmp_path / "feed",
        exported=[tree],
    )

    assert plan.eligible == []
    assert plan.retained[0].reason is not None
    assert "exported" in plan.retained[0].reason


def test_an_exported_path_is_eligible_once_the_operator_names_it(tmp_path) -> None:
    """The override is per-path and explicit, not a global switch."""
    channel = tmp_path / "feed" / "dev" / "local"
    data = b"pkg"
    digest = _pool(channel, data)
    _repo(channel, "target/qemux86-64", [digest])
    tree = tmp_path / "shared-workspace"
    source = _source(tree, "pkg-1.0.rpm", data)

    plan = plan_reclaim(
        [source],
        channel_root=channel,
        feed_root=tmp_path / "feed",
        exported=[tree],
        allow_exported=[tree],
    )

    assert [c.path for c in plan.eligible] == [source]


def test_the_plan_reports_reclaimable_bytes(tmp_path) -> None:
    """The preview carries a size, so the operator sees what it buys."""
    channel = tmp_path / "feed" / "dev" / "local"
    data = b"x" * 4096
    digest = _pool(channel, data)
    _repo(channel, "target/qemux86-64", [digest])
    source = _source(tmp_path / "tree", "pkg-1.0.rpm", data)

    plan = plan_reclaim([source], channel_root=channel, feed_root=tmp_path / "feed")

    assert plan.reclaimable_bytes == 4096
    assert source.exists()
