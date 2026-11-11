"""Metadata-only mirror mode and the marker keys every mirror run writes."""

from __future__ import annotations

import hashlib
import json
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


def _request(*, source_url: str, feed_root: Path, metadata_only: bool) -> feed_mirror.MirrorRequest:
    return feed_mirror.MirrorRequest(
        source_url=source_url,
        release="2024",
        channel="edge",
        feed_root=feed_root,
        repos=(_REPO,),
        metadata_only=metadata_only,
    )


def _channel_dir(feed_root: Path) -> Path:
    return feed_mod.channel_root(feed_root, release="2024", channel="edge")


def _marker(feed_root: Path) -> dict:
    return json.loads((_channel_dir(feed_root) / _REPO / _MARKER).read_text())


def test_metadata_only_downloads_no_payload(tmp_path: Path, feed_server) -> None:
    upstream = build_feed(tmp_path / "upstream", FeedSpec(repos={_REPO: 2}))
    server = feed_server(upstream)
    feed_root = tmp_path / "local-feed"

    result = feed_mirror.mirror(_request(source_url=server.url, feed_root=feed_root, metadata_only=True))

    local = _channel_dir(feed_root)
    assert not list(local.rglob("*.rpm"))
    for package in upstream.packages(_REPO):
        assert not (local / upstream.package_path(package).split("/2024/edge/", 1)[1]).exists()
    assert (local / _REPO / "repodata" / "repomd.xml").is_file()
    assert result.packages_downloaded == 0
    assert result.bytes_downloaded == 0


def test_metadata_only_marker_records_digest_and_mode(tmp_path: Path, feed_server) -> None:
    upstream = build_feed(tmp_path / "upstream", FeedSpec(repos={_REPO: 1}))
    server = feed_server(upstream)
    feed_root = tmp_path / "local-feed"

    feed_mirror.mirror(_request(source_url=server.url, feed_root=feed_root, metadata_only=True))

    repomd = (_channel_dir(feed_root) / _REPO / "repodata" / "repomd.xml").read_bytes()
    marker = _marker(feed_root)
    assert marker["mode"] == "metadata-only"
    assert marker["repomd_sha256"] == hashlib.sha256(repomd).hexdigest()
    assert set(marker) == {"source", "revision", "mirrored", "repomd_sha256", "mode"}


def test_full_mirror_marker_mode_is_full(tmp_path: Path, feed_server) -> None:
    upstream = build_feed(tmp_path / "upstream", FeedSpec(repos={_REPO: 1}))
    server = feed_server(upstream)
    feed_root = tmp_path / "local-feed"

    feed_mirror.mirror(_request(source_url=server.url, feed_root=feed_root, metadata_only=False))

    marker = _marker(feed_root)
    assert marker["mode"] == "full"
    assert len(marker["repomd_sha256"]) == 64


def test_metadata_only_writes_no_snapshot_pointer(tmp_path: Path, feed_server) -> None:
    upstream = build_feed(tmp_path / "upstream", FeedSpec(repos={_REPO: 1}))
    server = feed_server(upstream)
    feed_root = tmp_path / "local-feed"

    feed_mirror.mirror(_request(source_url=server.url, feed_root=feed_root, metadata_only=True))

    assert not list(_channel_dir(feed_root).rglob("snapshots-latest.json"))
