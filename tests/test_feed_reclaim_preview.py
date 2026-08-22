"""Tests that reclaim previews by default and deletes only what it previewed.

Two promises, and the second is subtler than it sounds.

Preview is the default. Every path into this module that does not carry an
explicit confirmation touches nothing, because the failure is irreversible and
lopsided - a retained duplicate costs disk, a wrongly deleted package costs a
multi-hour rebuild.

The deletion equals the preview. Not "matches" - equals, by operating on the
same plan rather than recomputing eligibility, so the two cannot drift. The
remaining gap is time: a plan can be minutes old and a build can rewrite a
package underneath it, so content is re-checked at the moment of removal and a
file whose bytes moved is skipped rather than deleted.
"""

from __future__ import annotations

import gzip
import hashlib
from typing import TYPE_CHECKING

import pytest

from bakar.feed_reclaim import apply_reclaim, plan_reclaim

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _feed(tmp_path: Path, contents: list[bytes]) -> tuple[Path, Path]:
    """A feed whose pool holds ``contents`` and whose repo references them all."""
    feed = tmp_path / "feed"
    channel = feed / "dev" / "local"
    shas = []
    for data in contents:
        digest = _sha(data)
        shas.append(digest)
        entry = channel / "_pkgs" / digest[:2] / f"{digest}.rpm"
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.write_bytes(data)

    repodata = channel / "target" / "qemux86-64" / "repodata"
    repodata.mkdir(parents=True)
    body = "".join(f'<package><location href="../../_pkgs/{s[:2]}/{s}.rpm"/></package>' for s in shas)
    with gzip.open(repodata / "live-primary.xml.gz", "wb") as fh:
        fh.write(f"<metadata>{body}</metadata>".encode())
    (repodata / "repomd.xml").write_text(
        '<repomd><data type="primary"><location href="repodata/live-primary.xml.gz"/></data></repomd>'
    )
    return feed, channel


def _sources(tmp_path: Path, contents: list[bytes]) -> list[Path]:
    """Source packages in a build tree, one per content."""
    rpm = tmp_path / "tree" / "build" / "tmp" / "deploy" / "rpm" / "core2_64"
    rpm.mkdir(parents=True)
    paths = []
    for index, data in enumerate(contents):
        path = rpm / f"pkg{index}-1.0-r0.core2_64.rpm"
        path.write_bytes(data)
        paths.append(path)
    return paths


def test_the_default_deletes_nothing(tmp_path) -> None:
    """Without an explicit confirmation, every source survives."""
    contents = [b"one", b"two"]
    feed, channel = _feed(tmp_path, contents)
    sources = _sources(tmp_path, contents)

    result = apply_reclaim(plan_reclaim(sources, channel_root=channel, feed_root=feed))

    assert result.applied is False
    assert all(s.is_file() for s in sources)


def test_the_default_still_reports_what_it_would_delete(tmp_path) -> None:
    """A preview that reports nothing is useless; it reports the full set."""
    contents = [b"one", b"two"]
    feed, channel = _feed(tmp_path, contents)
    sources = _sources(tmp_path, contents)

    result = apply_reclaim(plan_reclaim(sources, channel_root=channel, feed_root=feed))

    assert sorted(result.deleted) == sorted(sources)
    assert result.freed_bytes == sum(len(c) for c in contents)


def test_the_deletion_equals_the_preview(tmp_path) -> None:
    """With no intervening change, applying removes exactly the previewed set."""
    contents = [b"one", b"two", b"three"]
    feed, channel = _feed(tmp_path, contents)
    sources = _sources(tmp_path, contents)

    plan = plan_reclaim(sources, channel_root=channel, feed_root=feed)
    previewed = apply_reclaim(plan).deleted
    applied = apply_reclaim(plan, confirm=True)

    assert sorted(applied.deleted) == sorted(previewed)
    assert all(not s.exists() for s in sources)


def test_only_eligible_sources_are_deleted(tmp_path) -> None:
    """A retained candidate is never removed, even in the same run.

    The unpooled package here is the one a name-keyed or looser gate would take
    with the rest.
    """
    pooled = [b"pooled-one", b"pooled-two"]
    feed, channel = _feed(tmp_path, pooled)
    sources = _sources(tmp_path, [*pooled, b"never-pooled"])

    plan = plan_reclaim(sources, channel_root=channel, feed_root=feed)
    apply_reclaim(plan, confirm=True)

    assert not sources[0].exists()
    assert not sources[1].exists()
    assert sources[2].is_file()


def test_a_file_whose_content_changed_after_the_preview_is_skipped(tmp_path) -> None:
    """The promise is to delete the previewed bytes, not the path.

    A plan can be minutes old. If a build rewrote the package in between, the
    bytes the gate approved are gone and this path now holds something nobody
    checked - so it is skipped and reported rather than removed.
    """
    contents = [b"original"]
    feed, channel = _feed(tmp_path, contents)
    sources = _sources(tmp_path, contents)

    plan = plan_reclaim(sources, channel_root=channel, feed_root=feed)
    sources[0].write_bytes(b"rewritten by a later build")
    result = apply_reclaim(plan, confirm=True)

    assert result.deleted == []
    assert sources[0].is_file()
    assert "content changed" in result.skipped[0][1]


def test_a_file_that_vanished_after_the_preview_is_skipped(tmp_path) -> None:
    """An already-absent path is reported, not treated as an error."""
    contents = [b"one"]
    feed, channel = _feed(tmp_path, contents)
    sources = _sources(tmp_path, contents)

    plan = plan_reclaim(sources, channel_root=channel, feed_root=feed)
    sources[0].unlink()
    result = apply_reclaim(plan, confirm=True)

    assert result.deleted == []
    assert result.skipped[0][1] == "no longer present"


def test_freed_bytes_counts_only_what_was_actually_removed(tmp_path) -> None:
    """A skipped file contributes nothing to the freed figure.

    Reporting the planned total after a partial run would overstate what was
    reclaimed, and the operator would go looking for space that is still in use.
    """
    contents = [b"a" * 100, b"b" * 200]
    feed, channel = _feed(tmp_path, contents)
    sources = _sources(tmp_path, contents)

    plan = plan_reclaim(sources, channel_root=channel, feed_root=feed)
    sources[1].write_bytes(b"changed")
    result = apply_reclaim(plan, confirm=True)

    assert result.freed_bytes == 100


def test_a_fully_reclaimed_directory_is_reported(tmp_path) -> None:
    """A tree stripped of packages is named, because it must be rebuilt.

    Discovering that on the next build is worse than reading it in the result.
    """
    contents = [b"one", b"two"]
    feed, channel = _feed(tmp_path, contents)
    sources = _sources(tmp_path, contents)

    result = apply_reclaim(plan_reclaim(sources, channel_root=channel, feed_root=feed), confirm=True)

    assert result.emptied == [sources[0].parent]


def test_a_partially_reclaimed_directory_is_not_reported_as_emptied(tmp_path) -> None:
    """A directory that still holds packages is still usable, so it is not named."""
    pooled = [b"one"]
    feed, channel = _feed(tmp_path, pooled)
    sources = _sources(tmp_path, [*pooled, b"never-pooled"])

    result = apply_reclaim(plan_reclaim(sources, channel_root=channel, feed_root=feed), confirm=True)

    assert result.emptied == []


def test_retained_candidates_survive_into_the_result(tmp_path) -> None:
    """The result carries why things were kept, not just what went.

    A reclaim that reports only deletions leaves the operator unable to tell a
    clean run from one where the gate refused almost everything.
    """
    pooled = [b"one"]
    feed, channel = _feed(tmp_path, pooled)
    sources = _sources(tmp_path, [*pooled, b"never-pooled"])

    result = apply_reclaim(plan_reclaim(sources, channel_root=channel, feed_root=feed), confirm=True)

    assert len(result.retained) == 1
    assert "not in the pool" in result.retained[0].reason
