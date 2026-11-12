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
    from collections.abc import Callable
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
        # meta-avocado shape: bsp_root is a build-<stem> directory INSIDE the workspace.
        self.workspace = tmp_path / "ws"
        self.cfg = SimpleNamespace(
            workspace=self.workspace,
            bsp_root=self.workspace / "build-qemux86-64",
            resolved_tmpdir=self.tmpdir,
            machine="qemux86-64",
        )
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
        self.build_workspaces: list[object] = []
        self.guard_workspaces: list[object] = []
        self.build_exit = 0
        self.events: list[str] = []
        self.during_build: list[Callable[[], None]] = []

    def recipe(
        self, name: str, packages: dict[str, str] | None = None, *, layer: str = "meta-oe", version: str = "1.0"
    ) -> None:
        """Create pkgdata (``packages`` maps pkg -> rpm name), a recipe file and one rpm per package.

        The pkgdata recipe file carries ``PACKAGES`` only, as oe-core writes it:
        the layer cannot come from pkgdata, so it is found from the recipe file.
        """
        packages = packages or {name: name}
        recipe_dir = self.workspace / "sources" / layer / "recipes" / name
        recipe_dir.mkdir(parents=True, exist_ok=True)
        (recipe_dir / f"{name}_1.0.bb").write_text("")
        arch_dir = self.tmpdir / "pkgdata" / "avocado-qemux86-64"
        (arch_dir / "runtime").mkdir(parents=True, exist_ok=True)
        (arch_dir / name).write_text(f"PACKAGES: {' '.join(packages)}\n")
        for pkg, rpm_name in packages.items():
            (arch_dir / "runtime" / pkg).write_text(f"PKG:{pkg}: {rpm_name}\nPKGE: 0\n")
            _make_rpm(self.deploy, "core2_64", rpm_name, version=version, mtime=2_000_000)
        os.utime(self.deploy / "avocado-repo.map", (3_000_000, 3_000_000))

    def stamp(self, recipe: str, pv: str = "1.0") -> None:
        d = self.tmpdir / "stamps" / "core2-64" / recipe
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{pv}.do_package_write_rpm.{HASH}").write_text("")

    def builder(self, targets: list[str], **kw: object) -> SimpleNamespace:
        self.events.append("build")
        self.build_calls.append(list(targets))
        self.build_workspaces.append(kw.get("workspace"))
        for hook in self.during_build:
            hook()
        return SimpleNamespace(exit_code=self.build_exit, run_id="run-1")

    def run(self, req: refresh.RefreshRequest | None = None, **kw: object) -> refresh.RefreshResult:
        return refresh.run_refresh(req or self.req, builder=self.builder, recipe_exists=lambda _c, _r: True, **kw)  # type: ignore[arg-type]


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    w = World(tmp_path)

    def fake_live(path: Path, **_k: object) -> list:
        w.guard_workspaces.append(path)
        return []

    monkeypatch.setattr(run_discovery, "live_workspace_runs", fake_live)
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
    cfg = SimpleNamespace(workspace=tmp_path / "ws", bsp_root=tmp_path / "ws")
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


def test_build_guard_and_reads_use_one_workspace_for_a_meta_avocado_cfg(world: World) -> None:
    """bsp_root is ``<ws>/build-<stem>``; the build, the guard and the reads must not use it as the workspace."""
    world.recipe("htop")

    world.run()

    assert world.cfg.bsp_root != world.workspace
    assert world.build_workspaces == [world.workspace]
    assert set(world.guard_workspaces) == {world.workspace}
    # The delta was read from the cfg's own tmpdir, the one that build wrote.
    assert _delta_rpms(world) == ["htop-1.0-r0.1.core2_64.rpm"]


def test_find_recipe_follows_a_symlinked_layer_and_skips_tmp(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    real = tmp_path / "elsewhere" / "meta-avocado"
    (real / "recipes-x" / "foo").mkdir(parents=True)
    (real / "recipes-x" / "foo" / "foo_1.0.bb").write_text("")
    (ws / "sources").mkdir(parents=True)
    (ws / "sources" / "meta-avocado").symlink_to(real)
    build = ws / "build-qemux86-64"
    (build / "tmp" / "work").mkdir(parents=True)
    (build / "tmp" / "work" / "inside-tmp.bb").write_text("")
    cfg = SimpleNamespace(workspace=ws, bsp_root=build)

    found = refresh.find_recipe(cfg, "foo")  # type: ignore[arg-type]

    assert found is not None
    assert found.layer == "meta-avocado"
    assert found.path.name == "foo_1.0.bb"
    assert refresh.layer_has_recipe(cfg, "foo")  # type: ignore[arg-type]
    assert not refresh.layer_has_recipe(cfg, "inside-tmp")  # type: ignore[arg-type]


def test_layer_and_srcrev_come_from_the_recipe_file_not_pkgdata(world: World) -> None:
    world.recipe("htop", version="3.5.0+git0+0123abcd")
    (entry,) = world.run().entries
    assert (entry.layer, entry.layer_commit) == ("meta-oe", "abc123def456")
    assert entry.srcrev == "0123abcd"


def test_srcrev_is_none_for_a_release_version(world: World) -> None:
    world.recipe("htop")
    (entry,) = world.run().entries
    assert entry.srcrev is None


def test_layer_is_empty_when_the_recipe_cannot_be_located(world: World) -> None:
    world.recipe("htop")
    (entry,) = refresh.run_refresh(
        world.req,
        builder=world.builder,
        recipe_exists=lambda _c, _r: True,
        locate_recipe=lambda _c, _r: None,
    ).entries
    assert (entry.layer, entry.layer_commit) == ("", "")


def _delta_files(w: World) -> dict[str, bytes]:
    chan = feed.channel_root(w.req.delta_root, release="2026", channel="edge")
    return {str(p.relative_to(chan)): p.read_bytes() for p in sorted(chan.rglob("*")) if p.is_file()}


def test_failed_publish_keeps_the_previous_delta_whole(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    world.recipe("htop")
    world.run()
    before = _delta_files(world)
    assert feed_delta.DELTA_NAME in before

    def boom(*_a: object, **_k: object) -> dict:
        raise subprocess.CalledProcessError(1, "repo-stage-rpms.sh")

    monkeypatch.setattr(refresh.feed, "sync", boom)
    with pytest.raises(subprocess.CalledProcessError):
        world.run()

    assert _delta_files(world) == before
    assert not list(world.tmp.glob("delta*.new")), "the half-built sibling is removed"


def test_published_but_unrecordable_refresh_never_replaces_the_delta(world: World) -> None:
    """Two packages of one recipe at different versions: refused before the delta is touched."""
    world.recipe("htop", {"htop": "htop", "htop-doc": "htop-doc"})
    world.run()
    before = _delta_files(world)
    # htop-doc moves on, htop does not: the recipe's files now disagree.
    _make_rpm(world.deploy, "core2_64", "htop-doc", version="2.0", mtime=2_000_000)

    with pytest.raises(refresh.RefreshFailedError, match="disagree"):
        world.run()

    assert _delta_files(world) == before


def test_pr_bump_publishes_only_the_newest_build_of_a_package(world: World) -> None:
    world.recipe("htop")
    _make_rpm(world.deploy, "core2_64", "htop", release="r1.1", mtime=2_000_000)
    _make_rpm(world.deploy, "core2_64", "htop", release="r0.9", mtime=2_000_000)

    result = world.run()

    assert world.view_files == ["core2_64/htop-1.0-r1.1.core2_64.rpm"]  # type: ignore[attr-defined]
    assert _delta_rpms(world) == ["htop-1.0-r1.1.core2_64.rpm"]
    (entry,) = result.entries
    assert (entry.pv, entry.pr) == ("1.0", "r1.1")


def test_release_comparison_is_numeric_per_segment(world: World) -> None:
    world.recipe("htop")
    _make_rpm(world.deploy, "core2_64", "htop", release="r10.1", mtime=2_000_000)

    world.run()

    assert _delta_rpms(world) == ["htop-1.0-r10.1.core2_64.rpm"]


def test_dependency_rebuilt_in_this_run_does_not_make_the_map_stale(world: World) -> None:
    """A runtime dependency rebuilt by this build is newer than the map; it was not stale before."""
    world.recipe("htop")
    world.during_build.append(lambda: _make_rpm(world.deploy, "core2_64", "libncurses", mtime=4_000_000))

    world.run()

    assert _delta_rpms(world) == ["htop-1.0-r0.1.core2_64.rpm"]


def test_dependency_rewritten_in_this_run_is_not_counted_either(world: World) -> None:
    world.recipe("htop")
    _make_rpm(world.deploy, "core2_64", "libncurses", mtime=1_000_000)
    world.during_build.append(lambda: _make_rpm(world.deploy, "core2_64", "libncurses", mtime=4_000_000))

    world.run()

    assert _delta_rpms(world) == ["htop-1.0-r0.1.core2_64.rpm"]


def _git(repo: Path, *args: str) -> str:
    import subprocess

    done = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


def _real_shape_workspace(tmp_path: Path) -> SimpleNamespace:
    """Layers are siblings of the build directory and named by ``${TOPDIR}/../../`` tokens."""
    ws = tmp_path / "peridio-wrynose"
    build = ws / "build-qemux86-64-wrynose" / "build"
    (build / "conf").mkdir(parents=True)
    oe = ws / "meta-openembedded"
    (oe / "meta-oe" / "recipes-support" / "htop").mkdir(parents=True)
    (oe / "meta-oe" / "recipes-support" / "htop" / "htop_3.5.0.bb").write_text("")
    (oe / "meta-oe" / "tmp" / "work").mkdir(parents=True)
    (oe / "meta-oe" / "tmp" / "work" / "stray.bb").write_text("")
    avocado = ws / ".bakar" / "worktrees" / "wrynose" / "meta-avocado"
    (avocado / "meta-avocado" / "recipes-x" / "foo").mkdir(parents=True)
    (avocado / "meta-avocado" / "recipes-x" / "foo" / "foo.bb").write_text("")
    for repo in (oe, avocado):
        _git(repo, "init", "-q")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "x")
    (ws / "meta-atmel").mkdir()  # a layer with no recipes
    (build / "conf" / "bblayers.conf").write_text(
        'BBLAYERS ?= " \\\n'
        "    ${TOPDIR}/../../meta-atmel \\\n"
        "    ${TOPDIR}/../../.bakar/worktrees/wrynose/meta-avocado/meta-avocado \\\n"
        "    ${TOPDIR}/../../meta-openembedded/meta-oe \\\n"
        '    ${TOPDIR}/../../meta-gone \\\n"\n'
    )
    return SimpleNamespace(
        workspace=ws,
        bsp_root=ws / "build-qemux86-64-wrynose",
        bblayers_conf=build / "conf" / "bblayers.conf",
    )


def test_find_recipe_reads_layers_from_bblayers_conf(tmp_path: Path) -> None:
    cfg = _real_shape_workspace(tmp_path)

    found = refresh.find_recipe(cfg, "htop")  # type: ignore[arg-type]

    assert found is not None
    assert found.layer == "meta-oe"
    assert found.path == cfg.workspace.resolve() / "meta-openembedded/meta-oe/recipes-support/htop/htop_3.5.0.bb"
    assert found.repo_path == (cfg.workspace / "meta-openembedded").resolve()
    foo = refresh.find_recipe(cfg, "foo")  # type: ignore[arg-type]
    assert foo is not None
    assert foo.layer == "meta-avocado"
    assert foo.repo_path.name == "meta-avocado"
    assert foo.repo_path.parent.name == "wrynose"
    assert refresh.find_recipe(cfg, "stray") is None  # type: ignore[arg-type]
    assert not refresh.layer_has_recipe(cfg, "bash")  # type: ignore[arg-type]


def test_layer_commit_is_the_layer_repository_head(tmp_path: Path) -> None:
    cfg = _real_shape_workspace(tmp_path)
    found = refresh.find_recipe(cfg, "htop")  # type: ignore[arg-type]
    assert found is not None

    commit = refresh._layer_commit(found, [])

    assert commit == _git(cfg.workspace / "meta-openembedded", "rev-parse", "--short", "HEAD")
    assert commit != ""


def test_unknown_recipe_with_no_bblayers_conf_names_the_unbuilt_directory(world: World) -> None:
    world.cfg.bblayers_conf = world.workspace / "build-x" / "build" / "conf" / "bblayers.conf"

    with pytest.raises(refresh.RefreshFailedError, match=r"bblayers\.conf does not exist.*BAKAR_WORKTREE_BUILD_DIR"):
        refresh.run_refresh(world.req, builder=world.builder, recipe_exists=lambda _c, _r: False)

    assert world.build_calls == []
