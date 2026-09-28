"""Tests for target selection in ``bakar feed mirror`` (task 3.1).

Selection now reads the source's own ``targets.json`` when ``--target`` is
given: a target name resolves to the repository paths it locks, unioned with
any explicit ``--repo`` in first-seen order and de-duplicated. Every listed
path is validated the same way an operator-named one is, so a hostile index
entry fails before any package request. A repository the index declares but
the source never published (its own ``repomd.xml`` 404s) is skipped and
reported rather than failing the run - an explicitly named repository still
fails hard on a 404 (task 2.1's existing behavior).

Same fixture-import and config-isolation rules as task 2.1's
``test_feed_mirror.py``: the feed server comes in via ``pytest_plugins``,
never a named fixture import, and every test mocks
``bakar.commands.feed._resolve_cfg`` rather than touching the developer's
real ``~/.config/bakar/config.toml`` or feed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest import mock

import pytest
from typer.testing import CliRunner

from bakar.commands import app
from tests._feed_upstream import FeedSpec, build_feed

if TYPE_CHECKING:
    from pathlib import Path

pytest_plugins = ["tests._feed_upstream"]

pytestmark = pytest.mark.unit


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


def test_target_mirrors_exactly_the_repos_the_index_lists(cli: CliRunner, cfg, tmp_path: Path, feed_server) -> None:
    m1_repos = ["sdk/all", "target/m1", "sdk/m1", "target/m1-ext"]
    upstream = build_feed(
        tmp_path / "upstream",
        FeedSpec(repos=dict.fromkeys(m1_repos, 1), targets={"m1": m1_repos}),
    )
    server = feed_server(upstream)

    result = _invoke(cli, cfg, server.url, "--target", "m1")

    assert result.exit_code == 0, result.output
    channel_dir = cfg.effective_feed_dir / "2024" / "edge"
    for repo in m1_repos:
        assert (channel_dir / repo / "repodata" / "repomd.xml").is_file()
    for line in ("sdk/all", "target/m1", "sdk/m1", "target/m1-ext"):
        assert f"  {line}:" in result.output


def test_two_targets_sharing_a_repo_request_and_report_it_once(
    cli: CliRunner, cfg, tmp_path: Path, feed_server
) -> None:
    upstream = build_feed(
        tmp_path / "upstream",
        FeedSpec(
            repos={"sdk/all": 1, "target/m1": 1, "target/m2": 1},
            targets={"m1": ["sdk/all", "target/m1"], "m2": ["sdk/all", "target/m2"]},
        ),
    )
    server = feed_server(upstream)

    result = _invoke(cli, cfg, server.url, "--target", "m1", "--target", "m2")

    assert result.exit_code == 0, result.output
    repomd_requests = [p for p in server.requests if p == upstream.repomd_path("sdk/all")]
    assert len(repomd_requests) == 1
    assert result.output.count("  sdk/all:") == 1


def test_unknown_target_exits_1_naming_the_target_and_the_url(cli: CliRunner, cfg, tmp_path: Path, feed_server) -> None:
    upstream = build_feed(
        tmp_path / "upstream",
        FeedSpec(repos={"sdk/all": 1}, targets={"m1": ["sdk/all"]}),
    )
    server = feed_server(upstream)

    result = _invoke(cli, cfg, server.url, "--target", "bogus")

    assert result.exit_code == 1
    assert "bogus" in result.output
    assert "targets.json" in result.output


def test_missing_index_exits_1_mentioning_repo(cli: CliRunner, cfg, tmp_path: Path, feed_server) -> None:
    upstream = build_feed(tmp_path / "upstream", FeedSpec(repos={"sdk/all": 1}))
    server = feed_server(upstream)

    result = _invoke(cli, cfg, server.url, "--target", "m1")

    assert result.exit_code == 1
    assert "--repo" in result.output


def test_index_entry_traversal_refused_before_any_package_request(
    cli: CliRunner, cfg, tmp_path: Path, feed_server
) -> None:
    hostile = "target/../../outside"
    upstream = build_feed(
        tmp_path / "upstream",
        FeedSpec(repos={"sdk/all": 1}, targets={"m1": [hostile]}),
    )
    server = feed_server(upstream)

    result = _invoke(cli, cfg, server.url, "--target", "m1")

    assert result.exit_code == 1
    assert hostile in result.output
    assert "source index" in result.output
    assert server.package_requests == []


def test_index_declared_unpublished_repo_is_skipped_while_the_rest_mirror(
    cli: CliRunner, cfg, tmp_path: Path, feed_server
) -> None:
    upstream = build_feed(
        tmp_path / "upstream",
        FeedSpec(repos={"sdk/all": 1}, targets={"m1": ["sdk/all", "target/m1"]}),
    )
    server = feed_server(upstream)

    result = _invoke(cli, cfg, server.url, "--target", "m1")

    assert result.exit_code == 0, result.output
    assert "declared but not published: target/m1" in result.output
    channel_dir = cfg.effective_feed_dir / "2024" / "edge"
    assert (channel_dir / "sdk" / "all" / "repodata" / "repomd.xml").is_file()
    assert not (channel_dir / "target" / "m1").exists()
