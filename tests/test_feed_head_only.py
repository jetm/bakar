"""Head-only publish: heads render, nothing a client could pin is written."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from bakar import feed as feed_mod

pytestmark = pytest.mark.unit


@pytest.fixture
def calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    deploy = tmp_path / "rpm"
    deploy.mkdir()
    (deploy / "avocado-repo.map").write_text("repo=target/qemux86-64\n", encoding="utf-8")
    seen: list[str] = []
    monkeypatch.setattr(feed_mod, "stage_build", lambda **_kw: Path("/staged"))
    monkeypatch.setattr(feed_mod, "render_repo", lambda **kw: seen.append(kw["subpath"]))
    monkeypatch.setattr(Path, "is_dir", lambda self: True)
    return seen


def _publish(tmp_path: Path, **kw) -> feed_mod.FeedPublish:
    return feed_mod.FeedPublish(
        deploy_dir=tmp_path / "rpm",
        scripts=Path("/scripts"),
        release="dev",
        channel="local",
        **kw,
    )


def _roots(tmp_path: Path) -> feed_mod.FeedRoots:
    return feed_mod.FeedRoots(feed_root=tmp_path / "feed", stage_root=tmp_path / "stage")


def test_head_only_writes_no_pointer(tmp_path, calls) -> None:
    result = feed_mod.sync_paths(roots=_roots(tmp_path), publish=_publish(tmp_path, head_only=True))

    assert result["snapshot"] is None
    assert result["pointers"] == []
    assert result["sboms"] == []
    assert not list((tmp_path / "feed").rglob("snapshots-latest.json"))
    assert calls == ["target/qemux86-64"]


def test_head_only_skips_sbom_publication(tmp_path, calls, monkeypatch) -> None:
    sbom = mock.Mock()
    monkeypatch.setattr(feed_mod.feed_sbom, "publish_sbom", sbom)
    feed_mod.sync_paths(
        roots=_roots(tmp_path),
        publish=_publish(tmp_path, head_only=True, sboms=[tmp_path / "x.json"]),
    )

    sbom.assert_not_called()


def test_default_still_renders_snapshot_and_pointer(tmp_path, calls) -> None:
    result = feed_mod.sync_paths(roots=_roots(tmp_path), publish=_publish(tmp_path, snapshot="20260101T000000Z"))

    assert result["snapshot"] == "20260101T000000Z"
    assert calls == ["target/qemux86-64", "snapshots/20260101T000000Z/target/qemux86-64"]
    assert result["pointers"]


def test_sync_passes_caller_roots_and_snapshot(tmp_path) -> None:
    pub = _publish(tmp_path)
    roots = _roots(tmp_path)
    with mock.patch.object(feed_mod, "sync_paths") as sp:
        feed_mod.sync(
            mock.Mock(),
            publish=pub,
            feed_root=roots.feed_root,
            stage_root=roots.stage_root,
            snapshot="S1",
        )

    kw = sp.call_args.kwargs
    assert kw["roots"] == roots
    assert kw["publish"].snapshot == "S1"
