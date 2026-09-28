"""Tests for ``bakar feed mirror`` (single-threaded, explicit ``--repo`` only).

Task 2.1's tracer bullet: no retries, resume, disk preflight, target
selection, ownership guard or signatures - those are later tasks. Every test
here either mocks ``bakar.commands.feed._resolve_cfg`` or isolates ``HOME``
via ``monkeypatch``, per the task's own rule against touching the developer's
real ``~/.config/bakar/config.toml`` or feed.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest
from typer.testing import CliRunner

from bakar import feed_index, feed_mirror, feed_retention
from bakar.commands import app
from tests._feed_upstream import FeedSpec, build_feed

pytest_plugins = ["tests._feed_upstream"]

pytestmark = pytest.mark.unit


@pytest.fixture
def cli() -> CliRunner:
    return CliRunner()


@pytest.fixture
def cfg(tmp_path: Path):
    """A config stub exposing only ``effective_feed_dir``, like test_cli_feed.py."""
    feed_root = tmp_path / "feed"
    return mock.Mock(effective_feed_dir=feed_root, resolved_tmpdir=tmp_path / "build" / "tmp", workspace=tmp_path)


def _patch_cfg(cfg):
    return mock.patch("bakar.commands.feed._resolve_cfg", return_value=cfg)


def _invoke(cli: CliRunner, cfg, *args: str):
    with _patch_cfg(cfg):
        return cli.invoke(app, ["feed", "mirror", *args])


# --- happy path --------------------------------------------------------


def test_mirror_copies_a_repository_end_to_end(cli: CliRunner, cfg, tmp_path: Path, feed_server) -> None:
    upstream = build_feed(tmp_path / "upstream", FeedSpec(repos={"target/m1": 3}))
    server = feed_server(upstream)

    result = _invoke(cli, cfg, server.url, "--repo", "target/m1")

    assert result.exit_code == 0, result.output
    channel_dir = cfg.effective_feed_dir / "2024" / "edge"
    local_repomd = channel_dir / "target" / "m1" / "repodata" / "repomd.xml"
    assert local_repomd.is_file()
    assert local_repomd.read_bytes() == upstream.channel_dir.joinpath("target/m1/repodata/repomd.xml").read_bytes()

    for package in upstream.packages("target/m1"):
        assert (channel_dir / package.pool).is_file()

    dangling, unreadable = feed_retention.audit_references(channel_dir)
    assert dangling == []
    assert unreadable == []
    assert list(feed_index.derive_targets(channel_dir)) == ["m1"]
    assert "feed:" in result.output
    assert "downloaded: 3 packages" in result.output
    assert "next: bakar feed index --release 2024 --channel edge" in result.output


def test_mirror_reports_the_workspace_in_the_next_hint(cli: CliRunner, cfg, tmp_path: Path, feed_server) -> None:
    upstream = build_feed(tmp_path / "upstream", FeedSpec(repos={"sdk/all": 1}))
    server = feed_server(upstream)

    result = _invoke(cli, cfg, server.url, "--repo", "sdk/all", "-w", str(tmp_path))

    assert result.exit_code == 0, result.output
    # Rich may wrap the output line at the console width, inserting a bare
    # newline where a real terminal would just continue on the same row.
    unwrapped = result.output.replace("\n", "")
    assert f"next: bakar feed index --release 2024 --channel edge -w {tmp_path}" in unwrapped


# --- failures during phase 1 (metadata) --------------------------------


def test_metadata_checksum_mismatch_fails_before_any_package_request(
    cli: CliRunner, cfg, tmp_path: Path, feed_server
) -> None:
    upstream = build_feed(tmp_path / "upstream", FeedSpec(repos={"target/m1": 2}))
    server = feed_server(upstream)
    # Corrupt the primary's compressed bytes so its own declared checksum fails.
    repodata = tmp_path / "upstream" / "2024" / "edge" / "target" / "m1" / "repodata"
    primary = next(p for p in repodata.glob("*primary*"))
    upstream.corrupt(upstream.url_path(f"target/m1/repodata/{primary.name}"))

    result = _invoke(cli, cfg, server.url, "--repo", "target/m1")

    assert result.exit_code == 1
    assert "feed mirror failed:" in result.output
    assert server.package_requests == []
    assert not (cfg.effective_feed_dir / "2024" / "edge" / "target" / "m1" / "repodata" / "repomd.xml").exists()


def test_oversized_primary_refused_with_no_package_request(cli: CliRunner, cfg, tmp_path: Path, feed_server) -> None:
    upstream = build_feed(tmp_path / "upstream", FeedSpec(repos={"target/m1": 5}))
    server = feed_server(upstream)
    upstream.override_open_size("target/m1", 1)

    result = _invoke(cli, cfg, server.url, "--repo", "target/m1")

    assert result.exit_code == 1
    assert "target/m1/repodata" in result.output or "primary" in result.output.lower()
    assert server.package_requests == []


def test_unsupported_checksum_type_refused(cli: CliRunner, cfg, tmp_path: Path, feed_server) -> None:
    upstream = build_feed(tmp_path / "upstream", FeedSpec(repos={"target/m1": 2}))
    server = feed_server(upstream)
    upstream.set_checksum_type("target/m1", 0, "md5")

    result = _invoke(cli, cfg, server.url, "--repo", "target/m1")

    assert result.exit_code == 1
    assert "md5" in result.output


def test_hostile_href_refused(cli: CliRunner, cfg, tmp_path: Path, feed_server) -> None:
    upstream = build_feed(tmp_path / "upstream", FeedSpec(repos={"target/m1": 2}))
    server = feed_server(upstream)
    hostile = "../../../../etc/passwd.rpm"
    upstream.replace_href("target/m1", 0, hostile)

    result = _invoke(cli, cfg, server.url, "--repo", "target/m1")

    assert result.exit_code == 1
    assert hostile in result.output
    assert server.package_requests == []
    assert not Path("/etc/passwd.rpm").exists()


def test_source_scheme_refused(cli: CliRunner, cfg, tmp_path: Path, feed_server) -> None:
    upstream = build_feed(tmp_path / "upstream", FeedSpec(repos={"target/m1": 1}))
    server = feed_server(upstream)

    result = _invoke(cli, cfg, "file:///etc", "--repo", "target/m1")

    assert result.exit_code == 1
    assert server.requests == []


def test_operator_repo_traversal_refused(cli: CliRunner, cfg, tmp_path: Path, feed_server) -> None:
    upstream = build_feed(tmp_path / "upstream", FeedSpec(repos={"target/m1": 1}))
    server = feed_server(upstream)

    result = _invoke(cli, cfg, server.url, "--repo", "../../etc")

    assert result.exit_code == 1
    assert server.requests == []


def test_unreachable_source_fails_naming_the_url_no_traceback(cli: CliRunner, cfg) -> None:
    closed = "http://127.0.0.1:1"

    result = _invoke(cli, cfg, closed, "--repo", "target/m1")

    assert result.exit_code == 1
    assert closed in result.output
    assert "Traceback" not in result.output


def test_no_selection_exits_2(cli: CliRunner, cfg) -> None:
    result = _invoke(cli, cfg, "http://127.0.0.1:1")

    assert result.exit_code == 2


# --- failures during phase 3 (packages) --------------------------------


def test_package_checksum_mismatch_leaves_no_file_and_no_index(
    cli: CliRunner, cfg, tmp_path: Path, feed_server
) -> None:
    upstream = build_feed(tmp_path / "upstream", FeedSpec(repos={"target/m1": 3}))
    server = feed_server(upstream)
    bad_package = upstream.packages("target/m1")[0]
    upstream.corrupt(upstream.package_path(bad_package))

    result = _invoke(cli, cfg, server.url, "--repo", "target/m1")

    assert result.exit_code == 1
    assert bad_package.pool in result.output
    dest = cfg.effective_feed_dir / "2024" / "edge" / bad_package.pool
    assert not dest.exists()
    local_repomd = cfg.effective_feed_dir / "2024" / "edge" / "target" / "m1" / "repodata" / "repomd.xml"
    assert not local_repomd.exists()


def test_interrupted_run_leaves_no_index(cli: CliRunner, cfg, tmp_path: Path, feed_server) -> None:
    upstream = build_feed(tmp_path / "upstream", FeedSpec(repos={"target/m1": 3}))
    server = feed_server(upstream)

    first = _invoke(cli, cfg, server.url, "--repo", "target/m1")
    assert first.exit_code == 0, first.output
    local_repomd = cfg.effective_feed_dir / "2024" / "edge" / "target" / "m1" / "repodata" / "repomd.xml"
    first_bytes = local_repomd.read_bytes()

    replaced_href = upstream.packages("target/m1")[0].href
    (new_package,) = upstream.update("target/m1", replace=replaced_href)
    upstream.withhold(upstream.package_path(new_package))

    second = _invoke(cli, cfg, server.url, "--repo", "target/m1")

    assert second.exit_code == 1
    assert local_repomd.read_bytes() == first_bytes


# --- workspace resolution -----------------------------------------------


def test_dash_w_with_no_feed_config_mirrors_under_underscore_feed(
    cli: CliRunner, tmp_path: Path, monkeypatch, feed_server
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    upstream = build_feed(tmp_path / "upstream", FeedSpec(repos={"sdk/all": 1}))
    server = feed_server(upstream)
    ws = tmp_path / "ws"
    ws.mkdir()

    result = cli.invoke(app, ["feed", "mirror", server.url, "--repo", "sdk/all", "-w", str(ws)])

    assert result.exit_code == 0, result.output
    assert (ws / "_feed" / "2024" / "edge" / "sdk" / "all" / "repodata" / "repomd.xml").is_file()


# --- calling the library directly ---------------------------------------


def test_mirror_library_rejects_target_selection(tmp_path: Path, feed_server) -> None:
    """Target-index selection is task 3.1's job; this task only takes --repo."""
    upstream = build_feed(tmp_path / "upstream", FeedSpec(repos={"target/m1": 1}))
    server = feed_server(upstream)

    request = feed_mirror.MirrorRequest(
        source_url=server.url,
        release="2024",
        channel="edge",
        feed_root=tmp_path / "local-feed",
        targets=("m1",),
    )

    with pytest.raises(feed_mirror.MirrorError):
        feed_mirror.mirror(request)
