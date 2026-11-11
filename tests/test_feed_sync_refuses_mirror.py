"""``feed sync`` must not publish into a directory a mirror run created.

A mirror holds another publisher's metadata (possibly repodata only). Syncing a
local build into it would interleave two publishers' repositories under one
head, and a client could not tell which one a package came from.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bakar import feed as feed_mod
from bakar import feed_mirror

pytestmark = pytest.mark.unit


@pytest.fixture
def tree(tmp_path: Path) -> dict[str, Path]:
    deploy = tmp_path / "deploy" / "rpm"
    deploy.mkdir(parents=True)
    (deploy / "avocado-repo.map").write_text("target/qemux86-64\n", encoding="utf-8")
    return {"deploy": deploy, "feed": tmp_path / "_feed", "stage": tmp_path / "_feed-stage"}


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []

    def stage(**_kw):
        seen.append("stage")
        return Path("/staged")

    monkeypatch.setattr(feed_mod, "stage_build", stage)
    monkeypatch.setattr(feed_mod, "render_repo", lambda **_kw: seen.append("render"))
    return seen


def _sync(tree: dict[str, Path]) -> dict[str, object]:
    return feed_mod.sync_paths(
        roots=feed_mod.FeedRoots(feed_root=tree["feed"], stage_root=tree["stage"]),
        publish=feed_mod.FeedPublish(
            deploy_dir=tree["deploy"],
            scripts=Path("/scripts"),
            release="dev",
            channel="local",
            snapshot="20260101T000000Z",
        ),
    )


def _mark(tree: dict[str, Path], repo: str = "target/qemux86-64") -> Path:
    marker = tree["feed"] / "dev" / "local" / repo / feed_mod.MIRROR_MARKER
    marker.parent.mkdir(parents=True)
    marker.write_text('{"source": "http://x/dev/local/target/qemux86-64"}\n', encoding="utf-8")
    return marker


def test_marker_constant_is_shared_with_the_mirror() -> None:
    assert feed_mirror._MARKER == feed_mod.MIRROR_MARKER == ".bakar-mirror.json"


def test_mirror_marker_blocks_sync(tree, calls) -> None:
    marker = _mark(tree)

    with pytest.raises(feed_mod.MirrorMarkerError) as exc:
        _sync(tree)

    assert str(marker.parent) in str(exc.value)
    assert calls == []
    assert not (tree["feed"] / "dev" / "local" / "snapshots-latest.json").exists()


def test_marker_in_an_undeclared_repo_also_blocks(tree, calls) -> None:
    _mark(tree, "sdk/other")

    with pytest.raises(feed_mod.MirrorMarkerError):
        _sync(tree)

    assert calls == []


def test_unmarked_feed_root_still_syncs(tree, calls) -> None:
    (tree["feed"] / "dev" / "local" / "target" / "qemux86-64").mkdir(parents=True)

    result = _sync(tree)

    assert calls[0] == "stage"
    assert result["snapshot"] == "20260101T000000Z"
