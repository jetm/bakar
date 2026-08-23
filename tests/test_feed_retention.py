"""Retention tests.

Every fixture builds a real feed on disk - repomd.xml naming a real gzipped
primary that names real pool files - because the whole module is about telling a
live reference from a dead one, and a mock cannot be wrong in the way a stale
file on disk is wrong.

The unreadable-metadata cases use genuinely broken bytes rather than a patched
exception, because which exception a broken primary raises IS the bug: gzip's
bad-magic error subclasses OSError while a corrupt deflate payload raises
zlib.error, which does not.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from typing import TYPE_CHECKING

import pytest

from bakar import feed_retention

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


def _pool_add(channel: Path, content: bytes) -> str:
    """Write ``content`` into the channel pool and return its sha256."""
    digest = hashlib.sha256(content).hexdigest()
    entry = channel / "_pkgs" / digest[:2] / f"{digest}.rpm"
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_bytes(content)
    return digest


def _render(
    repo_root: Path, shas: list[str], *, depth: int, stale: int = 0, primary_bytes: bytes | None = None
) -> Path:
    """Write a repository whose live metadata names ``shas`` in the pool.

    ``depth`` is how many levels the repository sits below the channel root, so
    the pool reference is built the way the renderer builds it - by counting up
    from the repository to the channel. ``stale`` adds that many previous
    metadata generations, which the index does NOT name. ``primary_bytes``
    replaces the live primary's contents, for the corrupt-metadata cases.
    """
    repodata = repo_root / "repodata"
    repodata.mkdir(parents=True, exist_ok=True)
    up = "/".join([".."] * depth)

    locations = "\n".join(f'<location href="{up}/_pkgs/{sha[:2]}/{sha}.rpm"/>' for sha in shas)
    body = f"<metadata>{locations}</metadata>".encode()

    live_names = []
    for kind in ("primary", "other", "filelists"):
        name = f"{hashlib.sha256(body + kind.encode()).hexdigest()}-{kind}.xml.gz"
        payload = primary_bytes if (kind == "primary" and primary_bytes is not None) else gzip.compress(body)
        (repodata / name).write_bytes(payload)
        live_names.append(name)

    for generation in range(stale):
        for kind in ("primary", "other", "filelists"):
            old = f"{hashlib.sha256(f'{generation}{kind}'.encode()).hexdigest()}-{kind}.xml.gz"
            (repodata / old).write_bytes(gzip.compress(b"<metadata>stale</metadata>"))

    hrefs = "\n".join(f'<data><location href="repodata/{name}"/></data>' for name in live_names)
    (repodata / "repomd.xml").write_text(f"<repomd>{hrefs}</repomd>")
    return repodata


@pytest.fixture
def feed(tmp_path: Path) -> Path:
    """A feed root holding one channel with a head repo and three snapshots."""
    root = tmp_path / "feed"
    channel = root / "2024" / "edge"
    channel.mkdir(parents=True)

    shared = _pool_add(channel, b"shared package")
    _render(channel / "target" / "qemux86-64", [shared], depth=2)

    for stamp in ("20260801T000000Z", "20260802T000000Z", "20260803T000000Z"):
        only = _pool_add(channel, f"only in {stamp}".encode())
        _render(channel / "snapshots" / stamp / "target" / "qemux86-64", [shared, only], depth=4)

    (channel / "snapshots-latest.json").write_text(json.dumps({"id": "20260803T000000Z"}) + "\n")
    return root


def _channel(feed_root: Path) -> Path:
    return feed_root / "2024" / "edge"


# --- snapshot selection ----------------------------------------------------


def test_snapshots_are_listed_oldest_first(feed: Path) -> None:
    assert feed_retention.list_snapshots(_channel(feed)) == [
        "20260801T000000Z",
        "20260802T000000Z",
        "20260803T000000Z",
    ]


def test_keeping_one_snapshot_removes_the_two_older_ones(feed: Path) -> None:
    plan = feed_retention.plan_retention(_channel(feed), feed_root=feed, keep=1)

    assert plan.removed_snapshots == ["20260801T000000Z", "20260802T000000Z"]
    assert plan.kept_snapshots == ["20260803T000000Z"]


def test_the_pinned_snapshot_is_never_removed(feed: Path) -> None:
    """Pin the OLDEST snapshot, then ask to keep only one.

    Age alone would remove it. The pin has to override that, and it has to do so
    without consuming the by-age slot - so the newest survives too.
    """
    channel = _channel(feed)
    (channel / "snapshots-latest.json").write_text(json.dumps({"id": "20260801T000000Z"}) + "\n")

    plan = feed_retention.plan_retention(channel, feed_root=feed, keep=1)

    assert plan.pinned == "20260801T000000Z"
    assert "20260801T000000Z" not in plan.removed_snapshots
    assert plan.removed_snapshots == ["20260802T000000Z"]
    assert set(plan.kept_snapshots) == {"20260801T000000Z", "20260803T000000Z"}


def test_an_unparseable_pointer_removes_nothing(feed: Path) -> None:
    """The pin is unknown, not absent. Guessing would delete what clients use."""
    channel = _channel(feed)
    (channel / "snapshots-latest.json").write_text("{ this is not json")

    plan = feed_retention.plan_retention(channel, feed_root=feed, keep=1)

    assert plan.removed_snapshots == []
    assert plan.pinned is None


def test_an_absent_pointer_still_allows_retention(feed: Path) -> None:
    """Distinct from the unparseable case: nothing is pinned, so age decides."""
    channel = _channel(feed)
    (channel / "snapshots-latest.json").unlink()

    plan = feed_retention.plan_retention(channel, feed_root=feed, keep=1)

    assert plan.removed_snapshots == ["20260801T000000Z", "20260802T000000Z"]


def test_a_foreign_directory_does_not_consume_a_keep_slot(feed: Path) -> None:
    """A non-id name sorts after every timestamp, so it would evict a real one.

    With --keep 3 and three snapshots, nothing should be removed. A name like
    "keep-me" sorting into the keep window would push the oldest real snapshot
    out of it.
    """
    channel = _channel(feed)
    (channel / "snapshots" / "keep-me").mkdir()

    plan = feed_retention.plan_retention(channel, feed_root=feed, keep=3)

    assert plan.removed_snapshots == []
    assert "keep-me" not in plan.kept_snapshots
    assert plan.foreign_snapshot_entries == ["keep-me"]


def test_an_interrupted_syncs_partial_directory_is_reported_not_removed(feed: Path) -> None:
    channel = _channel(feed)
    (channel / "snapshots" / ".rsync-partial").mkdir()

    plan = feed_retention.plan_retention(channel, feed_root=feed, keep=1)

    assert ".rsync-partial" not in plan.removed_snapshots
    assert ".rsync-partial" in plan.foreign_snapshot_entries


def test_a_symlinked_snapshot_is_never_removed(feed: Path, tmp_path: Path) -> None:
    """Walking into it would empty the archive it points at, not the snapshot."""
    channel = _channel(feed)
    archive = tmp_path / "archive"
    archive.mkdir()
    (archive / "precious.rpm").write_bytes(b"do not delete me")
    (channel / "snapshots" / "20260101T000000Z").symlink_to(archive)

    plan = feed_retention.plan_retention(channel, feed_root=feed, keep=1)
    assert "20260101T000000Z" not in plan.removed_snapshots
    assert "20260101T000000Z" in plan.foreign_snapshot_entries

    feed_retention.apply_retention(plan, feed_root=feed, confirm=True)
    assert (archive / "precious.rpm").exists()


def test_rmtree_refuses_a_symlinked_root(tmp_path: Path) -> None:
    target = tmp_path / "real"
    target.mkdir()
    (target / "file").write_bytes(b"content")
    link = tmp_path / "link"
    link.symlink_to(target)

    assert feed_retention._rmtree(link) is False
    assert (target / "file").exists()


# --- pool references -------------------------------------------------------


def test_pool_entries_the_head_still_references_are_not_removed(feed: Path) -> None:
    """The shared package is in every snapshot AND in the head.

    Removing two snapshots must not orphan it, which is the failure a
    per-snapshot reference count would produce.
    """
    shared = hashlib.sha256(b"shared package").hexdigest()

    plan = feed_retention.plan_retention(_channel(feed), feed_root=feed, keep=1)

    assert not any(p.name == f"{shared}.rpm" for p in plan.orphan_pool)


def test_pool_entries_only_a_removed_snapshot_referenced_are_removed(feed: Path) -> None:
    gone = hashlib.sha256(b"only in 20260801T000000Z").hexdigest()

    plan = feed_retention.plan_retention(_channel(feed), feed_root=feed, keep=1)

    assert any(p.name == f"{gone}.rpm" for p in plan.orphan_pool)


def test_a_reference_from_another_release_retains_the_entry(feed: Path) -> None:
    """The reference scan spans the feed root, not one channel.

    A repository in another release resolving into this channel's pool must hold
    the entry, which a per-channel scan would never see.
    """
    borrowed = hashlib.sha256(b"only in 20260801T000000Z").hexdigest()

    # 2025/edge/target/qemux86-64 is 2 levels below its own channel, so `../..`
    # lands on 2025/edge - one more `..` pair reaches across to 2024/edge.
    other = feed / "2025" / "edge" / "target" / "qemux86-64"
    other.mkdir(parents=True)
    repodata = other / "repodata"
    repodata.mkdir()
    href = f"../../../../2024/edge/_pkgs/{borrowed[:2]}/{borrowed}.rpm"
    body = f'<metadata><location href="{href}"/></metadata>'.encode()
    (repodata / "aaa-primary.xml.gz").write_bytes(gzip.compress(body))
    (repodata / "repomd.xml").write_text('<repomd><data><location href="repodata/aaa-primary.xml.gz"/></data></repomd>')

    plan = feed_retention.plan_retention(_channel(feed), feed_root=feed, keep=1)

    assert not any(p.name == f"{borrowed}.rpm" for p in plan.orphan_pool)


# --- unreadable metadata refuses ------------------------------------------


def test_a_primary_with_bad_gzip_magic_suppresses_pool_reclaim(tmp_path: Path) -> None:
    """The proven data-loss case.

    The index parses, so the repository is live and published. Its primary is not
    gzip - which raises BadGzipFile, an OSError subclass, so an ordinary handler
    swallows it. If that reads as "references nothing", the repository's own
    package becomes an orphan and gets deleted while the feed still serves it.
    """
    root = tmp_path / "feed"
    channel = root / "2024" / "edge"
    channel.mkdir(parents=True)
    live = _pool_add(channel, b"a package the live repo needs")
    _render(channel / "target" / "qemux86-64", [live], depth=2, primary_bytes=b"this is not gzip at all")
    (channel / "snapshots-latest.json").write_text(json.dumps({"id": "20260803T000000Z"}) + "\n")

    plan = feed_retention.plan_retention(channel, feed_root=root, keep=1)

    assert plan.pool_reclaim_suppressed is True
    assert plan.orphan_pool == []
    assert len(plan.unreadable_primaries) == 1

    feed_retention.apply_retention(plan, feed_root=root, confirm=True)
    assert (channel / "_pkgs" / live[:2] / f"{live}.rpm").exists()


def test_a_primary_with_a_corrupt_deflate_payload_does_not_crash(tmp_path: Path) -> None:
    """zlib.error does NOT subclass OSError, so it escapes an OSError handler.

    Correct gzip magic, garbage payload. Before the fix this aborted the run with
    an unhandled zlib.error rather than refusing.
    """
    root = tmp_path / "feed"
    channel = root / "2024" / "edge"
    channel.mkdir(parents=True)
    live = _pool_add(channel, b"a package the live repo needs")
    _render(
        channel / "target" / "qemux86-64",
        [live],
        depth=2,
        primary_bytes=b"\x1f\x8b\x08\x00truncated-garbage",
    )

    plan = feed_retention.plan_retention(channel, feed_root=root, keep=1)

    assert plan.pool_reclaim_suppressed is True
    assert plan.orphan_pool == []


def test_the_audit_reports_an_unreadable_primary_rather_than_clean(tmp_path: Path) -> None:
    """An unreadable primary is a repository the audit did not check.

    Reporting it as clean is how a real break hides, so audit_clean must be
    False even though nothing dangles.
    """
    root = tmp_path / "feed"
    channel = root / "2024" / "edge"
    channel.mkdir(parents=True)
    live = _pool_add(channel, b"pkg")
    _render(channel / "target" / "qemux86-64", [live], depth=2, primary_bytes=b"not gzip")

    dangling, unreadable = feed_retention.audit_references(root)

    assert dangling == []
    assert len(unreadable) == 1


def test_the_audit_detects_a_genuinely_dangling_reference(tmp_path: Path) -> None:
    """The audit's whole purpose, and previously never exercised.

    A live primary naming a package that is not on disk must be reported.
    """
    root = tmp_path / "feed"
    channel = root / "2024" / "edge"
    channel.mkdir(parents=True)
    missing = hashlib.sha256(b"never written to the pool").hexdigest()
    _render(channel / "target" / "qemux86-64", [missing], depth=2)

    dangling, unreadable = feed_retention.audit_references(root)

    assert unreadable == []
    assert len(dangling) == 1
    assert dangling[0][1].name == f"{missing}.rpm"


def test_a_non_utf8_repomd_is_skipped_not_raised(tmp_path: Path) -> None:
    """read_text must not decode through the ambient locale.

    A repomd.xml holding a byte the locale cannot decode raises
    UnicodeDecodeError - a ValueError, so it escapes an OSError-only handler.
    """
    root = tmp_path / "feed"
    channel = root / "2024" / "edge"
    channel.mkdir(parents=True)
    repodata = channel / "target" / "qemux86-64" / "repodata"
    repodata.mkdir(parents=True)
    (repodata / "repomd.xml").write_bytes(b"\xff\xfe not decodable as utf-8")

    assert feed_retention.live_metadata(repodata) is None

    plan = feed_retention.plan_retention(channel, feed_root=root, keep=1)
    assert plan.unreadable_indexes == [repodata / "repomd.xml"]


# --- stale metadata --------------------------------------------------------


def test_stale_metadata_is_swept_and_live_metadata_is_kept(tmp_path: Path) -> None:
    """Two prior generations, matching what the live feed accumulated."""
    root = tmp_path / "feed"
    channel = root / "2024" / "edge"
    channel.mkdir(parents=True)
    sha = _pool_add(channel, b"pkg")
    repodata = _render(channel / "sdk" / "all", [sha], depth=2, stale=2)

    plan = feed_retention.plan_retention(channel, feed_root=root, keep=1)

    live = feed_retention.live_metadata(repodata)
    assert live is not None
    assert len(plan.stale_metadata) == 6
    assert not any(path in live for path in plan.stale_metadata)


def test_the_sweep_never_leaves_the_named_channel(tmp_path: Path) -> None:
    """`gc --channel edge` must not delete metadata in a channel it never named.

    The reference scan is feed-wide on purpose; the DELETION is not.
    """
    root = tmp_path / "feed"
    mine = root / "2024" / "edge"
    theirs = root / "2024" / "stable"
    mine.mkdir(parents=True)
    theirs.mkdir(parents=True)

    _render(mine / "sdk" / "all", [_pool_add(mine, b"mine")], depth=2, stale=1)
    other_repodata = _render(theirs / "sdk" / "all", [_pool_add(theirs, b"theirs")], depth=2, stale=1)

    plan = feed_retention.plan_retention(mine, feed_root=root, keep=1)

    assert plan.stale_metadata, "the named channel's own stale metadata should still be found"
    assert all(mine in path.parents or path.is_relative_to(mine) for path in plan.stale_metadata)

    feed_retention.apply_retention(plan, feed_root=root, confirm=True)
    assert len(list(other_repodata.iterdir())) == 7, "the other channel's repodata must be untouched"


def test_a_repository_with_an_unreadable_index_is_skipped_not_swept(tmp_path: Path) -> None:
    """An unreadable index makes every sibling look dead. Sweeping is the bug."""
    root = tmp_path / "feed"
    channel = root / "2024" / "edge"
    channel.mkdir(parents=True)
    sha = _pool_add(channel, b"pkg")
    repodata = _render(channel / "sdk" / "all", [sha], depth=2, stale=2)

    (repodata / "repomd.xml").unlink()

    plan = feed_retention.plan_retention(channel, feed_root=root, keep=1)

    assert plan.stale_metadata == []
    # The skip has to be REPORTED, not silently invisible: this directory holds
    # six metadata files that all look unreferenced with no index to check them
    # against, so it is the one an operator most needs named.
    assert plan.unreadable_indexes == [repodata / "repomd.xml"]
    assert feed_retention.live_metadata(repodata) is None


def test_a_stale_primary_that_parses_is_not_treated_as_live(tmp_path: Path) -> None:
    """The whole reason globbing is banned: a stale primary still parses.

    The stale generation here names a pool entry nothing live names. If the scan
    globbed for ``*primary.xml.gz`` that entry would look referenced and survive
    forever.
    """
    root = tmp_path / "feed"
    channel = root / "2024" / "edge"
    channel.mkdir(parents=True)
    live_sha = _pool_add(channel, b"live pkg")
    dead_sha = _pool_add(channel, b"dead pkg")

    repo = channel / "sdk" / "all"
    repodata = _render(repo, [live_sha], depth=2)

    dead_body = f'<metadata><location href="../../_pkgs/{dead_sha[:2]}/{dead_sha}.rpm"/></metadata>'.encode()
    (repodata / "0000-primary.xml.gz").write_bytes(gzip.compress(dead_body))

    plan = feed_retention.plan_retention(channel, feed_root=root, keep=1)

    assert any(p.name == f"{dead_sha}.rpm" for p in plan.orphan_pool)
    assert not any(p.name == f"{live_sha}.rpm" for p in plan.orphan_pool)
    assert repodata / "0000-primary.xml.gz" in plan.stale_metadata


# --- preview vs confirm ----------------------------------------------------


def test_preview_is_the_default_and_deletes_nothing(feed: Path) -> None:
    plan = feed_retention.plan_retention(_channel(feed), feed_root=feed, keep=1)
    before = sorted(p.relative_to(feed) for p in feed.rglob("*") if p.is_file())

    result = feed_retention.apply_retention(plan, feed_root=feed)

    assert result.applied is False
    assert result.removed_snapshots == plan.removed_snapshots
    assert sorted(p.relative_to(feed) for p in feed.rglob("*") if p.is_file()) == before


def test_a_confirmed_run_removes_exactly_what_it_previewed(feed: Path) -> None:
    channel = _channel(feed)
    plan = feed_retention.plan_retention(channel, feed_root=feed, keep=1)
    previewed_pool = list(plan.orphan_pool)
    previewed_meta = list(plan.stale_metadata)

    result = feed_retention.apply_retention(plan, feed_root=feed, confirm=True)

    assert result.applied is True
    assert result.removed_pool == previewed_pool
    assert result.removed_metadata == previewed_meta
    assert not (channel / "snapshots" / "20260801T000000Z").exists()
    assert (channel / "snapshots" / "20260803T000000Z").is_dir()
    assert all(not p.exists() for p in previewed_pool)


def test_no_reference_dangles_after_a_confirmed_run(feed: Path) -> None:
    """The property the whole module has to hold."""
    plan = feed_retention.plan_retention(_channel(feed), feed_root=feed, keep=1)

    result = feed_retention.apply_retention(plan, feed_root=feed, confirm=True)

    assert result.dangling == []
    assert result.audit_clean is True
    assert feed_retention.audit_references(feed) == ([], [])


def test_a_snapshot_that_could_not_be_removed_is_not_reported_as_removed(feed: Path, monkeypatch) -> None:
    """Reporting a failed deletion as done also counts its bytes as freed."""
    channel = _channel(feed)
    plan = feed_retention.plan_retention(channel, feed_root=feed, keep=1)
    assert plan.removed_snapshots

    monkeypatch.setattr(feed_retention, "_rmtree", lambda _root: False)
    result = feed_retention.apply_retention(plan, feed_root=feed, confirm=True)

    assert result.removed_snapshots == []
    assert result.failed_snapshots == plan.removed_snapshots
    assert (channel / "snapshots" / "20260801T000000Z").is_dir()


def test_a_run_with_nothing_to_do_says_so(tmp_path: Path) -> None:
    """A no-op must be distinguishable from a run that did not look."""
    root = tmp_path / "feed"
    channel = root / "2024" / "edge"
    channel.mkdir(parents=True)
    sha = _pool_add(channel, b"pkg")
    _render(channel / "target" / "qemux86-64", [sha], depth=2)

    plan = feed_retention.plan_retention(channel, feed_root=root, keep=1)

    assert plan.is_empty is True
    assert plan.reclaimable_bytes == 0


def test_a_feed_with_work_to_do_is_not_empty(feed: Path) -> None:
    plan = feed_retention.plan_retention(_channel(feed), feed_root=feed, keep=1)

    assert plan.is_empty is False
    assert plan.reclaimable_bytes > 0


def test_a_symlinked_pool_entry_is_never_offered_for_deletion(feed: Path, tmp_path: Path) -> None:
    """It points at content elsewhere; removing it is not this module's call."""
    channel = _channel(feed)
    elsewhere = tmp_path / "elsewhere.rpm"
    elsewhere.write_bytes(b"lives outside the feed")
    digest = hashlib.sha256(b"lives outside the feed").hexdigest()
    link = channel / "_pkgs" / digest[:2] / f"{digest}.rpm"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(elsewhere)

    plan = feed_retention.plan_retention(channel, feed_root=feed, keep=1)

    assert not any(p.name == f"{digest}.rpm" for p in plan.orphan_pool)
