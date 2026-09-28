"""Tests for the download phase of ``bakar feed mirror`` (task 5.1).

This task hardens phase 2 (packages) of the four-phase mirror: resume by
checksum, a disk-space preflight before any package request, retries across
every request the run makes (metadata and packages alike), and concurrent,
de-duplicated downloads through :data:`bakar.feed_mirror.MIRROR_WORKERS`
threads.

Same fixture-import and config-isolation rules as the sibling mirror test
modules: the feed server comes in via ``pytest_plugins``, never a named
fixture import, and every test mocks ``bakar.commands.feed._resolve_cfg``
rather than touching the developer's real ``~/.config/bakar/config.toml`` or
feed.
"""

from __future__ import annotations

import collections
import hashlib
import urllib.error
import urllib.request
from typing import TYPE_CHECKING
from unittest import mock

import pytest
from typer.testing import CliRunner

from bakar import feed_mirror
from bakar.commands import app
from tests._feed_upstream import FeedSpec, build_feed

if TYPE_CHECKING:
    from pathlib import Path

pytest_plugins = ["tests._feed_upstream"]

pytestmark = pytest.mark.unit

_DiskUsage = collections.namedtuple("_DiskUsage", "total used free")


@pytest.fixture
def cli() -> CliRunner:
    return CliRunner()


@pytest.fixture
def cfg(tmp_path: Path):
    """A config stub exposing only ``effective_feed_dir``, like test_feed_mirror.py."""
    feed_root = tmp_path / "feed"
    return mock.Mock(effective_feed_dir=feed_root, resolved_tmpdir=tmp_path / "build" / "tmp", workspace=tmp_path)


def _invoke(cli: CliRunner, cfg, *args: str):
    with mock.patch("bakar.commands.feed._resolve_cfg", return_value=cfg):
        return cli.invoke(app, ["feed", "mirror", *args])


# --- resume --------------------------------------------------------------


def test_resume_downloads_only_missing(cli: CliRunner, cfg, tmp_path: Path, feed_server) -> None:
    upstream = build_feed(tmp_path / "upstream", FeedSpec(repos={"target/m1": 5}))
    server = feed_server(upstream)

    first = _invoke(cli, cfg, server.url, "--repo", "target/m1")
    assert first.exit_code == 0, first.output

    channel_dir = cfg.effective_feed_dir / "2024" / "edge"
    packages = upstream.packages("target/m1")
    deleted = packages[:3]
    corrupted = packages[3]
    untouched = packages[4]

    for package in deleted:
        (channel_dir / package.pool).unlink()
    corrupted_dest = channel_dir / corrupted.pool
    corrupted_dest.write_bytes(b"not the real content")

    server.clear_requests()
    second = _invoke(cli, cfg, server.url, "--repo", "target/m1")

    assert second.exit_code == 0, second.output
    expected_requests = {upstream.package_path(p) for p in (*deleted, corrupted)}
    assert set(server.package_requests) == expected_requests
    assert upstream.package_path(untouched) not in server.package_requests
    assert "downloaded: 4 packages" in second.output
    assert "reused: 1" in second.output

    for package in (*deleted, corrupted):
        digest = hashlib.sha256((channel_dir / package.pool).read_bytes()).hexdigest()
        assert digest == package.sha256
    assert corrupted_dest.read_bytes() != b"not the real content"


# --- disk preflight --------------------------------------------------------


def test_disk_preflight_refuses(cli: CliRunner, cfg, tmp_path: Path, feed_server, monkeypatch) -> None:
    upstream = build_feed(tmp_path / "upstream", FeedSpec(repos={"target/m1": 2}))
    server = feed_server(upstream)

    monkeypatch.setattr(feed_mirror.shutil, "disk_usage", lambda path: _DiskUsage(total=1, used=1, free=1))

    result = _invoke(cli, cfg, server.url, "--repo", "target/m1")

    assert result.exit_code == 1
    assert "GiB" in result.output


def test_disk_preflight_boundary_is_free_less_than_required(
    cli: CliRunner, tmp_path: Path, feed_server, monkeypatch
) -> None:
    """Exercises the actual comparison, not just a trivially-insufficient value.

    `free == required` must proceed (there is exactly enough space); one byte
    less must refuse, naming both figures. A `<=` instead of `<`, or an
    off-by-one in `required`, would flip one of these two outcomes. Each half
    uses its own feed root and its own upstream so the ownership guard (a
    repo already mirrored from a different source) never interferes with the
    disk-preflight check under test.
    """
    upstream = build_feed(tmp_path / "upstream", FeedSpec(repos={"target/m1": 2}))
    required = sum(p.size for p in upstream.packages("target/m1"))
    exact_cfg = mock.Mock(effective_feed_dir=tmp_path / "feed-exact", resolved_tmpdir=tmp_path / "build" / "tmp")
    monkeypatch.setattr(
        feed_mirror.shutil, "disk_usage", lambda path: _DiskUsage(total=required, used=0, free=required)
    )
    server = feed_server(upstream)

    exact = _invoke(cli, exact_cfg, server.url, "--repo", "target/m1")

    assert exact.exit_code == 0, exact.output

    upstream2 = build_feed(tmp_path / "upstream2", FeedSpec(repos={"target/m1": 2}))
    short_cfg = mock.Mock(effective_feed_dir=tmp_path / "feed-short", resolved_tmpdir=tmp_path / "build" / "tmp")
    monkeypatch.setattr(
        feed_mirror.shutil, "disk_usage", lambda path: _DiskUsage(total=required, used=1, free=required - 1)
    )
    server2 = feed_server(upstream2)

    short = _invoke(cli, short_cfg, server2.url, "--repo", "target/m1")

    assert short.exit_code == 1
    assert "need" in short.output
    assert "have" in short.output
    assert server2.package_requests == []


# --- retries: connection/HTTP faults --------------------------------------


def test_fail_once_package_still_mirrors(cli: CliRunner, cfg, tmp_path: Path, feed_server) -> None:
    upstream = build_feed(tmp_path / "upstream", FeedSpec(repos={"target/m1": 1}))
    server = feed_server(upstream)
    package = upstream.packages("target/m1")[0]
    upstream.fail_once(upstream.package_path(package))

    result = _invoke(cli, cfg, server.url, "--repo", "target/m1")

    assert result.exit_code == 0, result.output
    assert server.package_requests.count(upstream.package_path(package)) == 2


def test_fail_once_repomd_still_mirrors(cli: CliRunner, cfg, tmp_path: Path, feed_server) -> None:
    upstream = build_feed(tmp_path / "upstream", FeedSpec(repos={"target/m1": 1}))
    server = feed_server(upstream)
    upstream.fail_once(upstream.repomd_path("target/m1"))

    result = _invoke(cli, cfg, server.url, "--repo", "target/m1")

    assert result.exit_code == 0, result.output
    assert server.requests.count(upstream.repomd_path("target/m1")) == 2


def test_always_failing_package_retried_exactly_three_times(cli: CliRunner, cfg, tmp_path: Path, feed_server) -> None:
    upstream = build_feed(tmp_path / "upstream", FeedSpec(repos={"target/m1": 1}))
    server = feed_server(upstream)
    package = upstream.packages("target/m1")[0]
    upstream.fail_always(upstream.package_path(package))

    result = _invoke(cli, cfg, server.url, "--repo", "target/m1")

    assert result.exit_code == 1
    assert upstream.package_path(package) in result.output.replace("\n", "")
    assert server.package_requests.count(upstream.package_path(package)) == 3


def test_missing_package_attempted_exactly_once(cli: CliRunner, cfg, tmp_path: Path, feed_server) -> None:
    upstream = build_feed(tmp_path / "upstream", FeedSpec(repos={"target/m1": 1}))
    server = feed_server(upstream)
    package = upstream.packages("target/m1")[0]
    upstream.withhold(upstream.package_path(package))

    result = _invoke(cli, cfg, server.url, "--repo", "target/m1")

    assert result.exit_code == 1
    assert upstream.package_path(package) in result.output.replace("\n", "")
    assert server.package_requests.count(upstream.package_path(package)) == 1


# --- de-duplication across repositories -----------------------------------


def test_shared_pool_file_requested_once(cli: CliRunner, cfg, tmp_path: Path, feed_server) -> None:
    upstream = build_feed(
        tmp_path / "upstream", FeedSpec(repos={"target/a": 1, "target/b": 1}, shared=("target/a", "target/b"))
    )
    server = feed_server(upstream)
    shared = upstream.packages("target/a")[-1]
    assert shared.sha256 == upstream.packages("target/b")[-1].sha256

    result = _invoke(cli, cfg, server.url, "--repo", "target/a", "--repo", "target/b")

    assert result.exit_code == 0, result.output
    assert server.package_requests.count(upstream.package_path(shared)) == 1
    # Each repo lists 2 packages (its own plus the shared one) for 4 listed
    # entries total, but only 3 are unique - the shared payload must not be
    # double-counted in the reported total just because two repos list it.
    assert "downloaded: 3 packages" in result.output


def test_conflicting_checksums_for_same_destination_fails_before_any_package_request(
    cli: CliRunner, cfg, tmp_path: Path, feed_server
) -> None:
    upstream = build_feed(
        tmp_path / "upstream", FeedSpec(repos={"target/a": 1, "target/b": 1}, shared=("target/a", "target/b"))
    )
    # index 1 is the shared package: each repo lists its own package at index 0,
    # then the one shared payload appended after it.
    upstream.set_checksum_type("target/b", 1, "sha1")
    shared = upstream.packages("target/a")[-1]
    server = feed_server(upstream)

    result = _invoke(cli, cfg, server.url, "--repo", "target/a", "--repo", "target/b")

    assert result.exit_code == 1
    assert shared.pool in result.output.replace("\n", "")
    assert server.package_requests == []


def test_package_landing_inside_sibling_selected_repository_refused(
    cli: CliRunner, cfg, tmp_path: Path, feed_server
) -> None:
    """``sdk`` listing a package under ``all/`` while ``sdk/all`` is also selected.

    ``confine_package_href`` alone would accept this - the package resolves
    to a path inside ``sdk``'s own tree by segment-prefix, and that check has
    no visibility into what else this run selected. The destination is also
    ``sdk/all``'s own repository directory, so the package would otherwise
    land there without ``sdk/all``'s own listing or ownership guard ever
    knowing.
    """
    upstream = build_feed(tmp_path / "upstream", FeedSpec(repos={"sdk": 1, "sdk/all": 1}))
    upstream.replace_href("sdk", 0, "all/evil.rpm")
    server = feed_server(upstream)

    result = _invoke(cli, cfg, server.url, "--repo", "sdk", "--repo", "sdk/all")

    assert result.exit_code == 1
    assert "sdk/all" in result.output
    assert server.package_requests == []


def test_package_transfer_exceeding_declared_size_is_retried_then_refused(
    cli: CliRunner, cfg, tmp_path: Path, feed_server
) -> None:
    """A response longer than the listing's declared size is capped, not trusted to stop on its own.

    Asserts the specific "exceeds its declared size" message rather than
    only the exit code and retry count - a checksum mismatch on the
    over-length body would produce the same exit code and the same retry
    count, so only the message distinguishes this from the pre-existing
    checksum-mismatch path.
    """
    upstream = build_feed(tmp_path / "upstream", FeedSpec(repos={"target/m1": 1}))
    server = feed_server(upstream)
    package = upstream.packages("target/m1")[0]
    upstream.corrupt(upstream.package_path(package), data=b"x" * (package.size + 1))

    result = _invoke(cli, cfg, server.url, "--repo", "target/m1")

    assert result.exit_code == 1
    assert "exceeds its declared size" in result.output
    assert server.package_requests.count(upstream.package_path(package)) == 3


def test_signature_fetch_failure_fails_before_any_repo_publishes(
    cli: CliRunner, cfg, tmp_path: Path, feed_server
) -> None:
    """A signature-fetch failure for one repository must not leave another repository half-published.

    The signature is fetched in phase 1 now, alongside every other metadata
    request - a failure there fails the whole run before phase 3 writes
    anything for ANY selected repository, including one whose own metadata
    was otherwise fine.
    """
    upstream = build_feed(tmp_path / "upstream", FeedSpec(repos={"target/a": 1, "target/b": 1}))
    upstream.fail_always(upstream.signature_path("target/b"))
    server = feed_server(upstream)

    result = _invoke(cli, cfg, server.url, "--repo", "target/a", "--repo", "target/b")

    assert result.exit_code == 1
    channel_dir = cfg.effective_feed_dir / "2024" / "edge"
    assert not (channel_dir / "target/a" / "repodata" / "repomd.xml").exists()
    assert not (channel_dir / "target/a" / ".bakar-mirror.json").exists()


def test_redirect_request_refuses_every_redirect() -> None:
    """The no-redirect handler raises rather than following a 3xx to another URL."""
    handler = feed_mirror._NoRedirectHandler()
    req = urllib.request.Request("http://example.test/repodata/repomd.xml")

    with pytest.raises(urllib.error.HTTPError, match="redirect"):
        handler.redirect_request(req, None, 302, "Found", {}, "file:///etc/passwd")


def test_opener_installs_the_no_redirect_handler() -> None:
    """Every request made through the module opener goes through the redirect refusal."""
    assert any(isinstance(h, feed_mirror._NoRedirectHandler) for h in feed_mirror._OPENER.handlers)


def test_resume_digest_read_failure_raises_mirror_error_not_bare_oserror(
    tmp_path: Path, feed_server, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A filesystem failure re-hashing an existing destination must not bypass cancel-on-first-failure.

    ``_download_all`` cancels the rest of the phase only on ``MirrorError`` -
    a bare ``OSError`` from the resume-check path would silently skip that
    cancellation instead of triggering it.
    """
    upstream = build_feed(tmp_path / "upstream", FeedSpec(repos={"target/m1": 1}))
    server = feed_server(upstream)
    feed_root = tmp_path / "feed"
    request = feed_mirror.MirrorRequest(
        source_url=server.url, release="2024", channel="edge", feed_root=feed_root, repos=("target/m1",)
    )
    first = feed_mirror.mirror(request)
    assert first.packages_downloaded == 1

    real_file_digest = feed_mirror.meta.file_digest

    def _raising_file_digest(path, checksum_type):
        # Only the package's own resume-check digest fails - a real metadata
        # file (repomd.xml, primary.xml.gz) still verifies normally, so the
        # failure is isolated to the code path this test targets.
        if str(path).endswith(".rpm"):
            raise OSError("simulated read failure")
        return real_file_digest(path, checksum_type)

    monkeypatch.setattr(feed_mirror.meta, "file_digest", _raising_file_digest)

    with pytest.raises(feed_mirror.MirrorError, match="cannot read"):
        feed_mirror.mirror(request)
