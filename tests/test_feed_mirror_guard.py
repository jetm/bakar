"""Tests for the ownership guard, provenance marker and signature publication.

Task 4.1 adds three things to ``bakar feed mirror``:

- an ownership guard, evaluated for every selected repository before its
  metadata is requested, that refuses to mirror over local feed content this
  run did not create;
- a per-repository provenance marker (``.bakar-mirror.json``), written in the
  publish step immediately before the signature and the index;
- publication of a source's detached ``repomd.xml.asc`` signature, written
  atomically before ``repomd.xml``, with a stale local signature removed when
  a same-source re-mirror finds the source no longer signs.

Same fixture-import and config-isolation rules as the sibling mirror test
modules: the feed server comes in via ``pytest_plugins``, never a named
fixture import, and library calls use a ``tmp_path`` feed root rather than
touching the developer's real bakar config or feed.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

import pytest

from bakar import feed as feed_mod
from bakar import feed_mirror
from tests._feed_upstream import FeedSpec, build_feed

if TYPE_CHECKING:
    from pathlib import Path

pytest_plugins = ["tests._feed_upstream"]

pytestmark = pytest.mark.unit

_REPO = "target/m1"
_MARKER = ".bakar-mirror.json"


def _request(*, source_url: str, feed_root: Path, repo: str = _REPO) -> feed_mirror.MirrorRequest:
    return feed_mirror.MirrorRequest(
        source_url=source_url,
        release="2024",
        channel="edge",
        feed_root=feed_root,
        repos=(repo,),
    )


def _channel_dir(feed_root: Path) -> Path:
    return feed_mod.channel_root(feed_root, release="2024", channel="edge")


# --- foreign repository refused -----------------------------------------


def test_foreign_repository_refused_with_no_marker(tmp_path: Path, feed_server) -> None:
    upstream = build_feed(tmp_path / "upstream", FeedSpec(repos={_REPO: 2}))
    server = feed_server(upstream)
    feed_root = tmp_path / "local-feed"

    local_repomd = _channel_dir(feed_root) / _REPO / "repodata" / "repomd.xml"
    local_repomd.parent.mkdir(parents=True)
    original = b"not a mirrored index\n"
    local_repomd.write_bytes(original)

    with pytest.raises(feed_mirror.MirrorError, match=_REPO):
        feed_mirror.mirror(_request(source_url=server.url, feed_root=feed_root))

    assert server.requests == []
    assert local_repomd.read_bytes() == original


def test_foreign_repository_refused_with_mismatched_marker(tmp_path: Path, feed_server) -> None:
    upstream = build_feed(tmp_path / "upstream", FeedSpec(repos={_REPO: 1}))
    server = feed_server(upstream)
    feed_root = tmp_path / "local-feed"

    local_repo = _channel_dir(feed_root) / _REPO
    (local_repo / "repodata").mkdir(parents=True)
    (local_repo / "repodata" / "repomd.xml").write_bytes(b"stale\n")
    other_source = "http://other.invalid/2024/edge/target/m1"
    (local_repo / _MARKER).write_text(json.dumps({"source": other_source}))

    expected_source = f"{server.url}/2024/edge/{_REPO}"
    with pytest.raises(feed_mirror.MirrorError) as excinfo:
        feed_mirror.mirror(_request(source_url=server.url, feed_root=feed_root))

    assert other_source in str(excinfo.value)
    assert expected_source in str(excinfo.value)
    assert server.requests == []


def test_same_source_remirror_succeeds_and_picks_up_a_changed_package(tmp_path: Path, feed_server) -> None:
    upstream = build_feed(tmp_path / "upstream", FeedSpec(repos={_REPO: 2}))
    server = feed_server(upstream)
    feed_root = tmp_path / "local-feed"

    first = feed_mirror.mirror(_request(source_url=server.url, feed_root=feed_root))
    assert first.repos[0].repo == _REPO

    replaced_href = upstream.packages(_REPO)[0].href
    (new_package,) = upstream.update(_REPO, replace=replaced_href)

    second = feed_mirror.mirror(_request(source_url=server.url, feed_root=feed_root))

    assert second.repos[0].repo == _REPO
    local_repomd = _channel_dir(feed_root) / _REPO / "repodata" / "repomd.xml"
    assert f"<revision>{upstream.revision(_REPO)}</revision>" in local_repomd.read_text()
    assert (_channel_dir(feed_root) / new_package.pool).is_file()


def test_stray_local_pointer_refused(tmp_path: Path, feed_server) -> None:
    upstream = build_feed(tmp_path / "upstream", FeedSpec(repos={_REPO: 1}))
    server = feed_server(upstream)
    feed_root = tmp_path / "local-feed"

    local_repo = _channel_dir(feed_root) / _REPO
    local_repo.mkdir(parents=True)
    pointer = local_repo / "snapshots-latest.json"
    pointer.write_text(json.dumps({"id": "stray"}))

    with pytest.raises(feed_mirror.MirrorError) as excinfo:
        feed_mirror.mirror(_request(source_url=server.url, feed_root=feed_root))

    assert str(pointer) in str(excinfo.value)
    assert server.requests == []


# --- marker content -------------------------------------------------------


def test_marker_names_source_and_revision(tmp_path: Path, feed_server) -> None:
    upstream = build_feed(tmp_path / "upstream", FeedSpec(repos={_REPO: 1}))
    server = feed_server(upstream)
    feed_root = tmp_path / "local-feed"

    feed_mirror.mirror(_request(source_url=server.url, feed_root=feed_root))

    marker_path = _channel_dir(feed_root) / _REPO / _MARKER
    marker = json.loads(marker_path.read_text())
    assert marker["source"] == f"{server.url}/2024/edge/{_REPO}"
    assert marker["revision"] == upstream.revision(_REPO)
    assert marker["mirrored"].endswith("Z")


# --- signature publication -------------------------------------------------


def test_signature_written_before_index(tmp_path: Path, feed_server, monkeypatch) -> None:
    upstream = build_feed(tmp_path / "upstream", FeedSpec(repos={_REPO: 1}, signed=(_REPO,)))
    server = feed_server(upstream)
    feed_root = tmp_path / "local-feed"

    replaced: list[str] = []
    real_replace = os.replace

    def _tracking_replace(src, dst, *args, **kwargs):
        replaced.append(str(dst))
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(feed_mirror.os, "replace", _tracking_replace)

    result = feed_mirror.mirror(_request(source_url=server.url, feed_root=feed_root))

    assert result.repos[0].signed is True
    asc_index = next(i for i, dest in enumerate(replaced) if dest.endswith("repomd.xml.asc"))
    repomd_index = next(i for i, dest in enumerate(replaced) if dest.endswith("repomd.xml"))
    assert asc_index < repomd_index
    local_asc = _channel_dir(feed_root) / _REPO / "repodata" / "repomd.xml.asc"
    assert local_asc.is_file()


def test_unsigned_source_leaves_no_asc(tmp_path: Path, feed_server) -> None:
    upstream = build_feed(tmp_path / "upstream", FeedSpec(repos={_REPO: 1}))
    server = feed_server(upstream)
    feed_root = tmp_path / "local-feed"

    result = feed_mirror.mirror(_request(source_url=server.url, feed_root=feed_root))

    assert result.repos[0].signed is False
    local_asc = _channel_dir(feed_root) / _REPO / "repodata" / "repomd.xml.asc"
    assert not local_asc.exists()


def test_stale_signature_removed_when_source_stops_signing(tmp_path: Path, feed_server) -> None:
    upstream = build_feed(tmp_path / "upstream", FeedSpec(repos={_REPO: 1}, signed=(_REPO,)))
    server = feed_server(upstream)
    feed_root = tmp_path / "local-feed"

    first = feed_mirror.mirror(_request(source_url=server.url, feed_root=feed_root))
    assert first.repos[0].signed is True
    local_asc = _channel_dir(feed_root) / _REPO / "repodata" / "repomd.xml.asc"
    assert local_asc.is_file()

    upstream.set_signed(_REPO, signed=False)

    second = feed_mirror.mirror(_request(source_url=server.url, feed_root=feed_root))

    assert second.repos[0].signed is False
    assert not local_asc.exists()
    local_repomd = _channel_dir(feed_root) / _REPO / "repodata" / "repomd.xml"
    assert local_repomd.read_bytes() == upstream.channel_dir.joinpath(f"{_REPO}/repodata/repomd.xml").read_bytes()


def test_signature_fetch_failure_raises_naming_the_url(tmp_path: Path, feed_server) -> None:
    upstream = build_feed(tmp_path / "upstream", FeedSpec(repos={_REPO: 1}))
    server = feed_server(upstream)
    feed_root = tmp_path / "local-feed"
    upstream.fail_always(upstream.signature_path(_REPO))

    with pytest.raises(feed_mirror.MirrorError) as excinfo:
        feed_mirror.mirror(_request(source_url=server.url, feed_root=feed_root))

    assert upstream.signature_path(_REPO) in str(excinfo.value)


# --- snapshot state never touched ------------------------------------------


def test_snapshot_state_never_requested(tmp_path: Path, feed_server) -> None:
    upstream = build_feed(
        tmp_path / "upstream",
        FeedSpec(repos={_REPO: 1}, snapshot="20240101T000000Z"),
    )
    server = feed_server(upstream)
    feed_root = tmp_path / "local-feed"

    feed_mirror.mirror(_request(source_url=server.url, feed_root=feed_root))

    channel_dir = _channel_dir(feed_root)
    assert not (channel_dir / _REPO / "snapshots-latest.json").exists()
    assert not (channel_dir / "snapshots").exists()
    assert not any("snapshots" in path for path in server.requests)
