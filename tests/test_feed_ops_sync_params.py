"""``sync_then_index`` parameters: roots, package subset, snapshot, index switch.

The package subset is applied through a filtered hardlink view of the deploy
dir. ``repo-stage-rpms.sh`` exits 1 when ``avocado-repo.map`` is missing and
only warns on a missing arch directory, so a view that drops the map (or an arch
directory the map names) turns a leaf-recipe publish into a hard failure.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from bakar import feed as feed_mod
from bakar import feed_filter, feed_ops

pytestmark = pytest.mark.unit


def _deploy(tmp_path: Path) -> Path:
    deploy = tmp_path / "deploy" / "rpm"
    for arch, names in {
        "core2_64": ["foo-1.0-r0.0.core2_64.rpm", "bar-lib-2.1-r0.1.core2_64.rpm"],
        "qemux86_64": ["baz-3.0-r1.qemux86_64.rpm"],
        "noarch": [],
    }.items():
        (deploy / arch).mkdir(parents=True)
        for n in names:
            (deploy / arch / n).write_bytes(n.encode())
    (deploy / "avocado-repo.map").write_text("target/qemux86-64 core2_64 qemux86_64\n")
    return deploy


def test_filtered_view_keeps_repo_map(tmp_path: Path) -> None:
    deploy = _deploy(tmp_path)
    view = feed_filter.build_filtered_view(deploy, tmp_path / "view", {"foo"})

    assert (view / "avocado-repo.map").read_text() == (deploy / "avocado-repo.map").read_text()


def test_filtered_view_keeps_only_named_packages_and_all_arch_dirs(tmp_path: Path) -> None:
    deploy = _deploy(tmp_path)
    view = feed_filter.build_filtered_view(deploy, tmp_path / "view", {"foo", "bar-lib"})

    assert sorted(p.name for p in (view / "core2_64").iterdir()) == [
        "bar-lib-2.1-r0.1.core2_64.rpm",
        "foo-1.0-r0.0.core2_64.rpm",
    ]
    assert (view / "qemux86_64").is_dir()
    assert list((view / "qemux86_64").iterdir()) == []
    assert (view / "noarch").is_dir()


def test_filtered_view_hardlinks_rather_than_copies(tmp_path: Path) -> None:
    deploy = _deploy(tmp_path)
    view = feed_filter.build_filtered_view(deploy, tmp_path / "view", {"foo"})

    src = deploy / "core2_64" / "foo-1.0-r0.0.core2_64.rpm"
    dst = view / "core2_64" / "foo-1.0-r0.0.core2_64.rpm"
    assert os.path.samefile(src, dst)


def test_filtered_view_matches_dashed_names_exactly(tmp_path: Path) -> None:
    """``bar`` must not select ``bar-lib``: the match is on the parsed name."""
    deploy = _deploy(tmp_path)
    view = feed_filter.build_filtered_view(deploy, tmp_path / "view", {"bar"})

    assert list((view / "core2_64").iterdir()) == []


def test_filtered_view_skips_unparseable_names(tmp_path: Path) -> None:
    deploy = _deploy(tmp_path)
    (deploy / "core2_64" / "junk.rpm").write_bytes(b"x")
    view = feed_filter.build_filtered_view(deploy, tmp_path / "view", {"junk"})

    assert list((view / "core2_64").iterdir()) == []


def test_filtered_view_without_map_is_an_error(tmp_path: Path) -> None:
    deploy = _deploy(tmp_path)
    (deploy / "avocado-repo.map").unlink()

    with pytest.raises(FileNotFoundError):
        feed_filter.build_filtered_view(deploy, tmp_path / "view", {"foo"})


@pytest.fixture
def cfg(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(resolved_tmpdir=tmp_path, effective_feed_dir=tmp_path / "_feed")


@pytest.fixture
def seen(monkeypatch: pytest.MonkeyPatch, cfg) -> dict[str, list]:
    log: dict[str, list] = {"sync": [], "index": []}
    sync_spec = create_autospec(feed_mod.sync, spec_set=True)

    def record_sync(cfg_arg, *, publish, feed_root=None, stage_root=None, snapshot=None):
        # Capture the view's content now: it is removed once sync returns.
        seen_files = sorted(
            p.relative_to(publish.deploy_dir).as_posix() for p in publish.deploy_dir.rglob("*") if p.is_file()
        )
        log["sync"].append(
            {
                "deploy_dir": publish.deploy_dir,
                "files": seen_files,
                "feed_root": feed_root,
                "stage_root": stage_root,
                "snapshot": snapshot,
            }
        )
        return {"channel_root": cfg_arg.resolved_tmpdir / "chan"}

    sync_spec.side_effect = record_sync
    index_spec = create_autospec(feed_ops.feed_index.write_targets_index, spec_set=True)
    index_spec.side_effect = lambda root: log["index"].append(root) or root / "targets.json"
    monkeypatch.setattr(feed_ops.feed, "sync", sync_spec)
    monkeypatch.setattr(feed_ops.feed_index, "write_targets_index", index_spec)
    monkeypatch.setattr(feed_ops.feed, "meta_avocado_scripts", lambda _y: Path("/scripts"))
    return log


def test_defaults_pass_no_extra_sync_keywords_and_index(seen, cfg, tmp_path) -> None:
    _deploy(tmp_path)
    feed_ops.sync_then_index(cfg, tmp_path / "m.yml")

    call = seen["sync"][0]
    assert call["feed_root"] is None and call["stage_root"] is None and call["snapshot"] is None
    assert call["deploy_dir"] == feed_ops.deploy_dir(cfg)
    assert len(seen["index"]) == 1


def test_params_forwarded_and_index_off(seen, cfg, tmp_path) -> None:
    _deploy(tmp_path)
    params = feed_ops.SyncParams(feed_root=tmp_path / "f", stage_root=tmp_path / "s", snapshot="snap1", index=False)
    result = feed_ops.sync_then_index(cfg, tmp_path / "m.yml", params=params)

    call = seen["sync"][0]
    assert (call["feed_root"], call["stage_root"], call["snapshot"]) == (
        tmp_path / "f",
        tmp_path / "s",
        "snap1",
    )
    assert seen["index"] == []
    assert "index" not in result


def test_only_packages_syncs_a_filtered_view_then_removes_it(seen, cfg, tmp_path) -> None:
    deploy = _deploy(tmp_path)
    assert deploy == feed_ops.deploy_dir(cfg)
    params = feed_ops.SyncParams(only_packages=frozenset({"foo"}))
    feed_ops.sync_then_index(cfg, tmp_path / "m.yml", params=params)

    call = seen["sync"][0]
    assert call["deploy_dir"] != deploy
    assert call["files"] == ["avocado-repo.map", "core2_64/foo-1.0-r0.0.core2_64.rpm"]
    assert not call["deploy_dir"].exists()
    assert (deploy / "core2_64" / "bar-lib-2.1-r0.1.core2_64.rpm").exists()


def test_view_is_removed_when_building_it_fails(seen, cfg, tmp_path) -> None:
    deploy = _deploy(tmp_path)
    (deploy / "avocado-repo.map").unlink()
    params = feed_ops.SyncParams(only_packages=frozenset({"foo"}))

    with pytest.raises(FileNotFoundError):
        feed_ops.sync_then_index(cfg, tmp_path / "m.yml", params=params)

    assert [p.name for p in deploy.parent.iterdir() if p.name.startswith("feed-view-")] == []
