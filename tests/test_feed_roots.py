"""Tests for feed root, stage root, script and repo-map resolution.

The stage-root assertions carry most of the weight here. A stage root that is
partitioned where the feed is shared makes each machine's render of the
release-global ``sdk/all`` repository REPLACE the previous one instead of
unioning with it - the feed still serves, dnf still resolves, and the toolchain
repository has quietly shrunk to whatever was staged last. Nothing errors, so
these are the only thing standing between that and a wrong feed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from bakar.feed import (
    DEFAULT_CHANNEL,
    DEFAULT_RELEASE,
    channel_root,
    meta_avocado_scripts,
    parse_repo_map,
    resolve_feed_root,
    resolve_stage_root,
)
from tests.conftest import make_build_config

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


def _map(tmp_path: Path, body: str) -> Path:
    """Write an ``avocado-repo.map`` and return its path."""
    path = tmp_path / "avocado-repo.map"
    path.write_text(body)
    return path


def test_resolve_feed_root_is_the_configured_feed_dir(tmp_path) -> None:
    """The served root is whatever the config resolved, not a second opinion."""
    cfg = make_build_config(workspace=tmp_path, feed_dir=str(tmp_path / "feed"))

    assert resolve_feed_root(cfg) == tmp_path / "feed"


def test_resolve_stage_root_defaults_beside_the_workspace_feed(tmp_path) -> None:
    """With no feed configured, staging is ``<workspace>/_feed-stage``."""
    cfg = make_build_config(workspace=tmp_path)

    assert resolve_stage_root(cfg) == tmp_path.resolve() / "_feed-stage"


def test_resolve_stage_root_is_identical_for_two_machines(tmp_path) -> None:
    """Two machines in one workspace stage into the SAME root.

    A per-machine stage root is the silent-truncation failure: the release-global
    toolchain repository is rendered from whatever tree it is handed, so a second
    machine rendering from its own tree replaces the first machine's packages
    rather than adding to them.
    """
    a = make_build_config(workspace=tmp_path, machine="qemux86-64")
    b = make_build_config(workspace=tmp_path, machine="imx93-frdm")

    assert resolve_stage_root(a) == resolve_stage_root(b)


def test_resolve_stage_root_follows_a_shared_feed_across_workspaces(tmp_path) -> None:
    """When the feed is shared, the stage root is shared too.

    Sharing the feed but not the stage root reintroduces the truncation across
    workspaces instead of across machines: each workspace would render ``sdk/all``
    from its own staged tree into the one shared channel root, and the last render
    wins. The stage root therefore derives from the feed root, not the workspace.
    """
    shared = str(tmp_path / "shared-feed")
    a = make_build_config(workspace=tmp_path / "ws-a", feed_dir=shared)
    b = make_build_config(workspace=tmp_path / "ws-b", feed_dir=shared)

    assert resolve_stage_root(a) == resolve_stage_root(b)


def test_resolve_stage_root_is_never_inside_the_served_root(tmp_path) -> None:
    """Staging must not sit under the feed root, or staged RPMs get served.

    The feed root is served verbatim by a static file server, so anything beneath
    it is public. A staged tree is pre-render input and has no repodata, so
    serving it would advertise packages no repository indexes.
    """
    cfg = make_build_config(workspace=tmp_path, feed_dir=str(tmp_path / "feed"))

    feed = resolve_feed_root(cfg)
    stage = resolve_stage_root(cfg)

    assert not stage.is_relative_to(feed)


def test_channel_root_composes_release_and_channel(tmp_path) -> None:
    """The channel root is ``<feed>/<release>/<channel>``."""
    assert channel_root(tmp_path) == tmp_path / DEFAULT_RELEASE / DEFAULT_CHANNEL


def test_channel_root_honors_an_explicit_release_and_channel(tmp_path) -> None:
    """Both segments are overridable; two releases are live in practice."""
    assert channel_root(tmp_path, release="2026", channel="edge") == tmp_path / "2026" / "edge"


def test_meta_avocado_scripts_resolves_through_the_repo_boundary(tmp_path) -> None:
    """A source kas YAML resolves to its sibling ``meta-avocado/scripts``."""
    scripts = tmp_path / "meta-avocado" / "scripts"
    scripts.mkdir(parents=True)
    yaml = tmp_path / "meta-avocado" / "kas" / "machine" / "qemux86-64.yml"
    yaml.parent.mkdir(parents=True)
    yaml.write_text("header: {}\n")

    assert meta_avocado_scripts(yaml) == scripts


def test_meta_avocado_scripts_raises_naming_the_path_it_wanted(tmp_path) -> None:
    """An absent scripts directory fails loudly and says where it looked.

    Proceeding without the scripts would render nothing and report success, so
    this raises rather than returning None - and the message carries the path
    because "meta-avocado not found" sends the reader hunting.
    """
    yaml = tmp_path / "meta-avocado" / "kas" / "machine" / "qemux86-64.yml"
    yaml.parent.mkdir(parents=True)
    yaml.write_text("header: {}\n")

    with pytest.raises(FileNotFoundError) as excinfo:
        meta_avocado_scripts(yaml)

    assert str(tmp_path / "meta-avocado" / "scripts") in str(excinfo.value)


def test_parse_repo_map_strips_the_releasever_prefix(tmp_path) -> None:
    """``repo=`` lines yield repo roots with the ``$releasever/`` prefix removed.

    The prefix is a placeholder the renderer substitutes, so carrying it into a
    subpath would produce a literal ``$releasever`` directory in the feed.
    """
    path = _map(
        tmp_path,
        "core2_64=$releasever/target/qemux86-64/core2_64\n"
        "repo=$releasever/sdk/all\n"
        "repo=$releasever/sdk/qemux86-64\n"
        "repo=$releasever/target/qemux86-64\n",
    )

    assert parse_repo_map(path) == ["sdk/all", "sdk/qemux86-64", "target/qemux86-64"]


def test_parse_repo_map_skips_extension_repositories(tmp_path) -> None:
    """A ``target/<machine>-ext`` repo is not staged from a build.

    Extension content comes from ``avocado ext package``, not BitBake, so staging
    it here would render an empty repository over whatever the extension flow had
    already published.
    """
    path = _map(
        tmp_path,
        "repo=$releasever/sdk/all\nrepo=$releasever/target/qemux86-64\nrepo=$releasever/target/qemux86-64-ext\n",
    )

    assert parse_repo_map(path) == ["sdk/all", "target/qemux86-64"]


def test_parse_repo_map_ignores_arch_mapping_and_comment_lines(tmp_path) -> None:
    """Only ``repo=`` lines are repo roots; arch mappings and comments are not."""
    path = _map(
        tmp_path,
        "# generated\nnoarch=$releasever/target/qemux86-64/noarch\n\nrepo=$releasever/sdk/all\n",
    )

    assert parse_repo_map(path) == ["sdk/all"]


def test_parse_repo_map_raises_naming_the_path_when_absent(tmp_path) -> None:
    """A missing map fails naming the path, rather than staging nothing quietly."""
    missing = tmp_path / "avocado-repo.map"

    with pytest.raises(FileNotFoundError) as excinfo:
        parse_repo_map(missing)

    assert str(missing) in str(excinfo.value)


def test_parse_repo_map_deduplicates_repeated_roots(tmp_path) -> None:
    """A root declared twice is staged once, in first-seen order."""
    path = _map(
        tmp_path,
        "repo=$releasever/sdk/all\nrepo=$releasever/target/qemux86-64\nrepo=$releasever/sdk/all\n",
    )

    assert parse_repo_map(path) == ["sdk/all", "target/qemux86-64"]
