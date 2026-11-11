"""Refresh pipeline: build, filtered publish, delta replacement and the manifests."""

from __future__ import annotations

import json
import os
import shutil
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from bakar import feed, feed_delta, layers, run_discovery
from bakar.commands import _feed_refresh as refresh

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit

HASH = "a" * 64


def _make_rpm(  # noqa: PLR0913
    deploy: Path,
    arch: str,
    name: str,
    version: str = "1.0",
    release: str = "r0.1",
    mtime: float = 0,
) -> Path:
    path = deploy / arch / f"{name}-{version}-{release}.{arch}.rpm"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"rpm")
    if mtime:
        os.utime(path, (mtime, mtime))
    return path


class World:
    """A fake workspace: tmpdir with deploy/pkgdata/stamps, a delta root, and call records."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.tmpdir = tmp_path / "tmpdir"
        self.deploy = self.tmpdir / "deploy" / "rpm"
        self.deploy.mkdir(parents=True)
        (self.deploy / "avocado-repo.map").write_text("core2_64=$releasever/target/qemux86-64/core2_64\n")
        self.cfg = SimpleNamespace(bsp_root=tmp_path / "ws", resolved_tmpdir=self.tmpdir, machine="qemux86-64")
        self.feed_root = tmp_path / "feed"
        self.req = refresh.RefreshRequest(
            cfg=self.cfg,  # type: ignore[arg-type]
            kas_yaml=tmp_path / "ws" / "meta-avocado" / "kas" / "m.yml",
            recipes=("htop",),
            feed_root=self.feed_root,
            sync_stage_root=tmp_path / "feed-stage",
            delta_root=tmp_path / "delta",
            work_root=tmp_path / "delta-work",
            release="2026",
            channel="edge",
            base_release="2025",
            base_channel="stable",
            base_snapshot_id="snap-1",
        )
        self.build_calls: list[list[str]] = []
        self.build_exit = 0
        self.events: list[str] = []

    def recipe(self, name: str, packages: dict[str, str] | None = None, *, layer: str = "meta-oe") -> None:
        """Create pkgdata (``packages`` maps pkg -> rpm name) and one rpm per package."""
        packages = packages or {name: name}
        arch_dir = self.tmpdir / "pkgdata" / "avocado-qemux86-64"
        (arch_dir / "runtime").mkdir(parents=True, exist_ok=True)
        (arch_dir / name).write_text(
            f"PACKAGES: {' '.join(packages)}\nFILE_DIRNAME: /ws/layers/{layer}/recipes/{name}\n"
        )
        for pkg, rpm_name in packages.items():
            (arch_dir / "runtime" / pkg).write_text(f"PKG:{pkg}: {rpm_name}\nPKGE: 0\n")
            _make_rpm(self.deploy, "core2_64", rpm_name, mtime=2_000_000)
        os.utime(self.deploy / "avocado-repo.map", (3_000_000, 3_000_000))

    def stamp(self, recipe: str, pv: str = "1.0") -> None:
        d = self.tmpdir / "stamps" / "core2-64" / recipe
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{pv}.do_package_write_rpm.{HASH}").write_text("")

    def builder(self, targets: list[str], **_kw: object) -> SimpleNamespace:
        self.events.append("build")
        self.build_calls.append(list(targets))
        return SimpleNamespace(exit_code=self.build_exit, run_id="run-1")

    def run(self, req: refresh.RefreshRequest | None = None, **kw: object) -> refresh.RefreshResult:
        return refresh.run_refresh(req or self.req, builder=self.builder, recipe_exists=lambda _c, _r: True, **kw)  # type: ignore[arg-type]


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    w = World(tmp_path)
    monkeypatch.setattr(run_discovery, "live_workspace_runs", lambda *_a, **_k: [])
    monkeypatch.setattr(refresh.feed_ops, "scripts_dir", lambda _y: tmp_path / "scripts")
    monkeypatch.setattr(
        layers,
        "collect_layer_hashes",
        lambda _cfg: [layers.LayerHash(repo="meta-oe", short_hash="abc123def456", branch="")],
    )

    def fake_sync(_cfg: object, *, publish: feed.FeedPublish, feed_root: Path, stage_root: Path, **_kw: object) -> dict:
        w.events.append("sync")
        w.head_only = publish.head_only  # type: ignore[attr-defined]
        w.view_files = sorted(str(p.relative_to(publish.deploy_dir)) for p in publish.deploy_dir.rglob("*.rpm"))  # type: ignore[attr-defined]
        chan = feed.channel_root(feed_root, release=publish.release, channel=publish.channel)
        # Like the real sync, publishing is additive: packages already there stay.
        for rpm in publish.deploy_dir.rglob("*.rpm"):
            dest = chan / "target" / rpm.parent.name / rpm.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(rpm, dest)
        return {"snapshot": None if publish.head_only else "snap-x", "channel_root": chan}

    monkeypatch.setattr(refresh.feed, "sync", fake_sync)
    return w


def _delta_rpms(w: World) -> list[str]:
    chan = feed.channel_root(w.req.delta_root, release="2026", channel="edge")
    return sorted(p.name for p in chan.rglob("*.rpm"))


def _with(w: World, **over: object) -> refresh.RefreshRequest:
    import dataclasses

    return dataclasses.replace(w.req, **over)  # type: ignore[arg-type]


def test_second_refresh_leaves_no_stale_packages(world: World) -> None:
    world.recipe("htop")
    world.recipe("nano")
    world.run(_with(world, recipes=("htop", "nano")))
    assert _delta_rpms(world) == ["htop-1.0-r0.1.core2_64.rpm", "nano-1.0-r0.1.core2_64.rpm"]

    world.run(_with(world, recipes=("htop",)))

    assert _delta_rpms(world) == ["htop-1.0-r0.1.core2_64.rpm"]


def test_only_named_recipes_are_staged(world: World) -> None:
    world.recipe("htop")
    _make_rpm(world.deploy, "core2_64", "bash", mtime=2_000_000)

    world.run()

    assert world.view_files == ["core2_64/htop-1.0-r0.1.core2_64.rpm"]  # type: ignore[attr-defined]
    assert world.build_calls == [["htop"]]


def test_renamed_package_is_selected_by_its_rpm_name(world: World) -> None:
    world.recipe("glibc", {"glibc": "libc6", "glibc-dev": "libc6-dev"})

    world.run(_with(world, recipes=("glibc",)))

    assert _delta_rpms(world) == ["libc6-1.0-r0.1.core2_64.rpm", "libc6-dev-1.0-r0.1.core2_64.rpm"]


def test_no_recipe_builds_nothing(world: World) -> None:
    with pytest.raises(refresh.RefreshFailedError, match="at least one recipe"):
        world.run(_with(world, recipes=()))
    assert world.build_calls == []


def test_unknown_recipe_fails_before_build_and_names_it(world: World) -> None:
    world.recipe("htop")
    with pytest.raises(refresh.RefreshFailedError, match="nosuch"):
        refresh.run_refresh(
            _with(world, recipes=("htop", "nosuch")),
            builder=world.builder,
            recipe_exists=lambda _c, r: r != "nosuch",
        )
    assert world.build_calls == []
    assert not world.req.delta_root.exists()


def test_layer_has_recipe_matches_exact_and_versioned_names(tmp_path: Path) -> None:
    rdir = tmp_path / "ws" / "layers" / "meta-oe" / "recipes-support" / "htop"
    rdir.mkdir(parents=True)
    (rdir / "htop_3.5.0.bb").write_text("")
    (tmp_path / "ws" / "layers" / "meta-oe" / "recipes-support" / "nano.bb").write_text("")
    cfg = SimpleNamespace(bsp_root=tmp_path / "ws")
    assert refresh.layer_has_recipe(cfg, "htop")  # type: ignore[arg-type]
    assert refresh.layer_has_recipe(cfg, "nano")  # type: ignore[arg-type]
    assert not refresh.layer_has_recipe(cfg, "ht")  # type: ignore[arg-type]
    assert not refresh.layer_has_recipe(cfg, "bash")  # type: ignore[arg-type]


def test_failed_build_keeps_previous_delta(world: World) -> None:
    world.recipe("htop")
    world.run()
    before = _delta_rpms(world)
    world.build_exit = 2
    with pytest.raises(refresh.RefreshFailedError, match="build failed"):
        world.run()
    assert _delta_rpms(world) == before


def test_guard_refusal_happens_before_build(world: World) -> None:
    world.recipe("htop")
    with pytest.raises(refresh.RefreshRefusedError, match="synced feed root"):
        world.run(_with(world, delta_root=world.feed_root))
    assert world.build_calls == []


def test_default_publish_is_head_only_and_snapshot_option_mints(world: World) -> None:
    world.recipe("htop")
    assert world.run().sync["snapshot"] is None
    assert world.head_only is True  # type: ignore[attr-defined]
    result = world.run(_with(world, snapshot=True))
    assert result.sync["snapshot"] == "snap-x"
    assert world.head_only is False  # type: ignore[attr-defined]


def test_manifests_record_entry_and_base(world: World) -> None:
    world.recipe("htop")
    world.stamp("htop")

    result = world.run()

    chan = result.channel_dir
    delta = json.loads((chan / feed_delta.DELTA_NAME).read_text())
    assert delta["base"] == {"release": "2025", "channel": "stable", "snapshot_id": "snap-1"}
    assert delta["built_recipes"] == ["htop"]
    assert delta["bakar_run_id"] == "run-1"
    assert delta["delta_path"] == str(chan)
    (entry,) = json.loads((chan / feed_delta.RECIPE_MAP_NAME).read_text())["entries"]
    assert entry["recipe"] == "htop"
    assert entry["packages"] == ["htop"]
    assert (entry["pv"], entry["pr"], entry["epoch"]) == ("1.0", "r0.1", 0)
    assert (entry["layer"], entry["layer_commit"]) == ("meta-oe", "abc123def456")
    assert entry["machine"] == "qemux86-64"
    assert entry["release"] == "2026"
    assert entry["task_hash"] == HASH


def test_unreadable_task_hash_is_null(world: World) -> None:
    world.recipe("htop")
    (entry,) = world.run().entries
    assert entry.task_hash is None


def test_missing_packages_for_a_recipe_refuses_and_keeps_delta(world: World) -> None:
    world.recipe("htop")
    world.run()
    before = _delta_rpms(world)
    with pytest.raises(refresh.RefreshFailedError, match="no packages for ghost"):
        world.run(_with(world, recipes=("htop", "ghost")))
    assert _delta_rpms(world) == before


def test_stale_map_against_preexisting_packages_refuses_before_wipe(world: World) -> None:
    world.recipe("htop")
    world.run()
    before = _delta_rpms(world)
    # A package from an earlier build is newer than the map.
    _make_rpm(world.deploy, "core2_64", "older-build", mtime=4_000_000)
    with pytest.raises(refresh.RefreshFailedError, match=r"avocado-repo\.map"):
        world.run()
    assert _delta_rpms(world) == before


def test_map_older_than_own_packages_is_not_stale(world: World) -> None:
    """Spike 1.2: a leaf build never rewrites the map, so it predates the recipe's own rpms."""
    world.recipe("htop")
    os.utime(world.deploy / "avocado-repo.map", (1_000_000, 1_000_000))
    world.run()
    assert _delta_rpms(world) == ["htop-1.0-r0.1.core2_64.rpm"]


def test_lock_is_released_after_a_refresh(world: World) -> None:
    world.recipe("htop")
    world.run()
    world.run()
