"""Refresh pipeline: build, filtered publish, delta replacement and the manifests."""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest

from bakar import feed, feed_delta, layers, run_discovery
from bakar.commands import _feed_refresh as refresh

if TYPE_CHECKING:
    from collections.abc import Callable

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
        (self.deploy / "avocado-repo.map").write_text(
            "repo=$releasever/target/qemux86-64\ncore2_64=$releasever/target/qemux86-64/core2_64\n"
        )
        # meta-avocado shape: bsp_root is a build-<stem> directory INSIDE the workspace.
        self.workspace = tmp_path / "ws"
        self.cfg = SimpleNamespace(
            workspace=self.workspace,
            bsp_root=self.workspace / "build-qemux86-64",
            resolved_tmpdir=self.tmpdir,
            machine="avocado-qemux86-64",  # MACHINE / pkg arch, not the target slug
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
        return refresh.run_refresh(req or self.req, builder=self.builder, **kw)  # type: ignore[arg-type]


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
        staged = feed.channel_root(stage_root, release=publish.release, channel=publish.channel)
        # Like the real sync: RPMs are staged per repo root, the delta gets the
        # pooled shape (repodata only, RPMs in _pkgs), and publishing is additive.
        for rpm in publish.deploy_dir.rglob("*.rpm"):
            dest = staged / "target" / "qemux86-64" / rpm.parent.name / rpm.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(rpm, dest)
            pooled = chan / "_pkgs" / "09" / f"{rpm.name}.sha"
            pooled.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(rpm, pooled.with_suffix(".rpm"))
        (chan / "target" / "qemux86-64" / "repodata").mkdir(parents=True, exist_ok=True)
        (chan / "target" / "qemux86-64" / "repodata" / "pooled-primary.xml.gz").write_bytes(b"pooled")
        return {
            "snapshot": None if publish.head_only else "snap-x",
            "channel_root": chan,
            "repos": ["target/qemux86-64"],
        }

    def fake_createrepo(repo: Path) -> None:
        """Stand-in for createrepo_c: a primary whose hrefs are relative to ``repo``."""
        w.createrepo_calls.append(repo)  # type: ignore[attr-defined]
        hrefs = "".join(f'<location href="{p.relative_to(repo)}"/>' for p in sorted(repo.rglob("*.rpm")))
        (repo / "repodata").mkdir(exist_ok=True)
        (repo / "repodata" / "primary.xml").write_text(hrefs)

    w.createrepo_calls = []  # type: ignore[attr-defined]
    monkeypatch.setattr(refresh.feed, "sync", fake_sync)
    monkeypatch.setattr(refresh, "_createrepo", fake_createrepo)
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
        )
    assert world.build_calls == []
    assert not world.req.delta_root.exists()


def test_find_recipe_matches_exact_and_versioned_names(tmp_path: Path) -> None:
    rdir = tmp_path / "ws" / "layers" / "meta-oe" / "recipes-support" / "htop"
    rdir.mkdir(parents=True)
    (rdir / "htop_3.5.0.bb").write_text("")
    (tmp_path / "ws" / "layers" / "meta-oe" / "recipes-support" / "nano.bb").write_text("")
    cfg = SimpleNamespace(workspace=tmp_path / "ws", bsp_root=tmp_path / "ws")
    assert refresh.find_recipe(cfg, "htop") is not None  # type: ignore[arg-type]
    assert refresh.find_recipe(cfg, "nano") is not None  # type: ignore[arg-type]
    assert refresh.find_recipe(cfg, "ht") is None  # type: ignore[arg-type]
    assert refresh.find_recipe(cfg, "bash") is None  # type: ignore[arg-type]


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
    ghost_dir = world.workspace / "sources" / "meta-oe" / "recipes" / "ghost"
    ghost_dir.mkdir(parents=True)
    (ghost_dir / "ghost_1.0.bb").write_text("")
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


def test_non_utf8_repo_map_is_a_refresh_failure(world: World) -> None:
    world.recipe("htop")
    world.run()
    before = _delta_files(world)
    (world.deploy / "avocado-repo.map").write_bytes(b"core2_64 \xff\n")
    with pytest.raises(refresh.RefreshFailedError, match=r"avocado-repo\.map") as info:
        world.run()
    assert "previous delta is untouched" in str(info.value)
    assert _delta_files(world) == before


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
    assert refresh.find_recipe(cfg, "foo") is not None  # type: ignore[arg-type]
    assert refresh.find_recipe(cfg, "inside-tmp") is None  # type: ignore[arg-type]


def test_layer_and_srcrev_come_from_the_recipe_file_not_pkgdata(world: World) -> None:
    world.recipe("htop", version="3.5.0+git0+0123abcd")
    (entry,) = world.run().entries
    assert (entry.layer, entry.layer_commit) == ("meta-oe", "abc123def456")
    assert entry.srcrev == "0123abcd"


def test_srcrev_is_none_for_a_release_version(world: World) -> None:
    world.recipe("htop")
    (entry,) = world.run().entries
    assert entry.srcrev is None


def test_layer_commit_is_null_when_git_and_the_collector_cannot_answer(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.recipe("htop")
    monkeypatch.setattr(layers, "collect_layer_hashes", lambda _cfg: [])
    (entry,) = world.run().entries
    assert (entry.layer, entry.layer_commit) == ("meta-oe", None)
    (written,) = json.loads(
        (
            feed.channel_root(world.req.delta_root, release="2026", channel="edge") / feed_delta.RECIPE_MAP_NAME
        ).read_text()
    )["entries"]
    assert (written["layer"], written["layer_commit"]) == ("meta-oe", None)


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
    assert found.layer == "meta-oe"  # the layer directory; the map records the repository
    assert found.path == cfg.workspace.resolve() / "meta-openembedded/meta-oe/recipes-support/htop/htop_3.5.0.bb"
    assert found.repo_path == (cfg.workspace / "meta-openembedded").resolve()
    foo = refresh.find_recipe(cfg, "foo")  # type: ignore[arg-type]
    assert foo is not None
    assert foo.layer == "meta-avocado"
    assert foo.repo_path.name == "meta-avocado"
    assert foo.repo_path.parent.name == "wrynose"
    assert refresh.find_recipe(cfg, "stray") is None  # type: ignore[arg-type]
    assert refresh.find_recipe(cfg, "bash") is None  # type: ignore[arg-type]


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
        refresh.run_refresh(world.req, builder=world.builder, locate_recipe=lambda _c, _r: [])

    assert world.build_calls == []


def test_delta_repositories_are_self_contained_after_a_refresh(world: World) -> None:
    world.recipe("htop")

    result = world.run()

    repo = result.channel_dir / "target" / "qemux86-64"
    assert not (result.channel_dir / "_pkgs").exists(), "no pool outside the repository directories"
    assert not (repo / "repodata" / "pooled-primary.xml.gz").exists(), "the pooled metadata is replaced"
    # Rendered in the fresh sibling, then swapped into place.
    assert [c.relative_to(world.req.delta_root.with_name("delta.new")) for c in world.createrepo_calls] == [  # type: ignore[attr-defined]
        repo.relative_to(world.req.delta_root)
    ]
    primary = (repo / "repodata" / "primary.xml").read_text()
    hrefs = re.findall(r'href="([^"]+)"', primary)
    assert hrefs == ["core2_64/htop-1.0-r0.1.core2_64.rpm"]
    for href in hrefs:
        assert (repo / href).is_file()
        assert ".." not in href


def test_snapshot_publish_makes_the_snapshot_copy_self_contained_too(world: World) -> None:
    world.recipe("htop")

    result = world.run(_with(world, snapshot=True))

    snap = result.channel_dir / "snapshots" / "snap-x" / "target" / "qemux86-64"
    assert (snap / "core2_64" / "htop-1.0-r0.1.core2_64.rpm").is_file()
    assert any(c.parts[-4:] == ("snapshots", "snap-x", "target", "qemux86-64") for c in world.createrepo_calls)  # type: ignore[attr-defined]


@pytest.mark.skipif(
    shutil.which("createrepo_c") is None or shutil.which("rpmbuild") is None,
    reason="needs createrepo_c and rpmbuild to build a real repository",
)
def test_real_createrepo_hrefs_resolve_inside_the_repository_directory(tmp_path: Path) -> None:
    import gzip
    import subprocess

    spec = tmp_path / "htop.spec"
    spec.write_text(
        "Name: htop\nVersion: 3.5.0\nRelease: r0.1\nSummary: t\nLicense: MIT\nBuildArch: noarch\n"
        "%description\nt\n%install\nmkdir -p %{buildroot}/usr/share\necho hi > %{buildroot}/usr/share/htop.txt\n"
        "%files\n/usr/share/htop.txt\n"
    )
    subprocess.run(
        ["rpmbuild", "-bb", "--define", f"_topdir {tmp_path}/top", "--define", f"_rpmdir {tmp_path}/out", str(spec)],
        check=True,
        capture_output=True,
    )
    stage = tmp_path / "stage" / "2026" / "edge"
    (stage / "target" / "m" / "noarch").mkdir(parents=True)
    shutil.copy2(next((tmp_path / "out").rglob("*.rpm")), stage / "target" / "m" / "noarch")
    chan = tmp_path / "delta" / "2026" / "edge"
    (chan / "_pkgs").mkdir(parents=True)

    refresh.make_repos_self_contained(chan, stage, {"snapshot": None, "repos": ["target/m"]})

    repo = chan / "target" / "m"
    (primary,) = (repo / "repodata").glob("*primary.xml.gz")
    hrefs = re.findall(r'<location href="([^"]+)"', gzip.decompress(primary.read_bytes()).decode())
    assert hrefs == ["noarch/htop-3.5.0-r0.1.noarch.rpm"]
    assert all((repo / h).resolve().is_relative_to(repo.resolve()) and (repo / h).is_file() for h in hrefs)
    assert not (chan / "_pkgs").exists()


def test_recipe_map_machine_is_the_target_slug_of_the_published_directory(world: World) -> None:
    world.recipe("htop")
    assert world.cfg.machine == "avocado-qemux86-64"

    result = world.run()

    (entry,) = json.loads((result.channel_dir / feed_delta.RECIPE_MAP_NAME).read_text())["entries"]
    target_dirs = [p.name for p in (result.channel_dir / "target").iterdir() if p.is_dir()]
    assert target_dirs == ["qemux86-64"]
    assert entry["machine"] == target_dirs[0]


@pytest.mark.parametrize(
    ("machine", "repos", "expected"),
    [
        ("avocado-qemux86-64", ["target/qemux86-64"], "qemux86-64"),
        ("avocado-qemux86-64", ["target/raspberrypi5", "target/qemux86-64"], "qemux86-64"),
        ("qemux86-64", ["target/qemux86-64"], "qemux86-64"),
        ("avocado-qemux86-64", [], "avocado-qemux86-64"),
    ],
)
def test_target_slug_comes_from_the_repo_map(tmp_path: Path, machine: str, repos: list[str], expected: str) -> None:
    deploy = tmp_path / "rpm"
    deploy.mkdir()
    (deploy / "avocado-repo.map").write_text("".join(f"repo=$releasever/{r}\n" for r in repos))
    assert refresh.target_slug(machine, deploy) == expected
    assert refresh.target_slug(machine, tmp_path / "no-map") == machine


def test_entry_layer_is_the_layer_repository_name_not_the_layer_directory(world: World) -> None:
    world.recipe("htop")
    repo = world.workspace / "meta-openembedded"
    loc = refresh.RecipeLocation(path=repo / "meta-oe" / "htop_1.0.bb", layer="meta-oe", repo_path=repo)

    (entry,) = refresh.run_refresh(
        world.req,
        builder=world.builder,
        locate_recipe=lambda _c, _r: [loc],
    ).entries

    assert loc.layer == "meta-oe"
    assert entry.layer == "meta-openembedded"


def _mirror_marker_during_build(w: World, root: Path) -> None:
    chan = feed.channel_root(root, release="2026", channel="edge") / "target" / "qemux86-64"

    def plant() -> None:
        chan.mkdir(parents=True, exist_ok=True)
        (chan / feed.MIRROR_MARKER).write_text("{}")

    w.during_build.append(plant)


@pytest.mark.parametrize("which", ["delta_root", "work_root"])
def test_mirror_aimed_at_the_channel_during_the_build_is_not_wiped(world: World, which: str) -> None:
    world.recipe("htop")
    _mirror_marker_during_build(world, getattr(world.req, which))

    with pytest.raises(refresh.RefreshRefusedError, match="mirror"):
        world.run()

    marker = (
        feed.channel_root(getattr(world.req, which), release="2026", channel="edge")
        / "target"
        / "qemux86-64"
        / feed.MIRROR_MARKER
    )
    assert marker.is_file()


def test_target_only_recipe_leaves_no_sdk_directory(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    world.recipe("htop")
    real_sync = refresh.feed.sync

    def sync_with_sdk(cfg: object, **kw: object) -> dict:
        result = real_sync(cfg, **kw)  # type: ignore[arg-type]
        chan = Path(str(result["channel_root"]))
        (chan / "sdk" / "qemux86-64" / "repodata").mkdir(parents=True)
        (chan / "sdk" / "qemux86-64" / "repodata" / "pooled-primary.xml.gz").write_bytes(b"pooled")
        return {**result, "repos": [*cast("list[str]", result["repos"]), "sdk/qemux86-64"]}

    monkeypatch.setattr(refresh.feed, "sync", sync_with_sdk)

    result = world.run()

    assert not (result.channel_dir / "sdk").exists()
    assert result.sync["repos"] == ["target/qemux86-64"]
    assert (result.channel_dir / "target" / "qemux86-64" / "core2_64" / "htop-1.0-r0.1.core2_64.rpm").is_file()


def _two_layer_loc(world: World) -> list[refresh.RecipeLocation]:
    out = []
    for name in ("meta-first", "meta-second"):
        repo = world.workspace / name
        (repo / "recipes").mkdir(parents=True, exist_ok=True)
        out.append(refresh.RecipeLocation(path=repo / "recipes" / "htop_1.0.bb", layer=name, repo_path=repo))
    return out


def test_recipe_in_several_layers_records_the_first_and_warns_naming_all(world: World) -> None:
    world.recipe("htop")
    locs = _two_layer_loc(world)

    result = refresh.run_refresh(
        world.req, builder=world.builder, locate_recipe=lambda _c, _r: locs, resolve_provider=lambda *_: None
    )

    (entry,) = result.entries
    assert entry.layer == "meta-first"
    (warning,) = result.warnings
    assert "htop" in warning
    assert "meta-first" in warning
    assert "meta-second" in warning


def test_recipe_in_several_layers_writes_a_null_layer_commit(world: World) -> None:
    # pkgdata carries no recipe path (PKGDATA_VARS has no FILE_DIRNAME), so the
    # providing layer cannot be told apart and no commit is guessed.
    world.recipe("htop")
    locs = _two_layer_loc(world)

    result = refresh.run_refresh(
        world.req, builder=world.builder, locate_recipe=lambda _c, _r: locs, resolve_provider=lambda *_: None
    )

    (entry,) = result.entries
    assert (entry.layer, entry.layer_commit) == ("meta-first", None)
    assert "layer_commit" in result.warnings[0]


def test_layers_of_one_repository_share_its_known_commit(world: World) -> None:
    world.recipe("htop")
    repo = world.workspace / "meta-oe"
    (repo / "recipes").mkdir(parents=True, exist_ok=True)
    locs = [
        refresh.RecipeLocation(path=repo / "recipes" / "htop_1.0.bb", layer=name, repo_path=repo)
        for name in ("meta-oe", "meta-python")
    ]

    result = refresh.run_refresh(
        world.req, builder=world.builder, locate_recipe=lambda _c, _r: locs, resolve_provider=lambda *_: None
    )

    (entry,) = result.entries
    assert (entry.layer, entry.layer_commit) == ("meta-oe", "abc123def456")
    (warning,) = result.warnings
    assert "meta-oe" in warning
    assert "meta-python" in warning


def test_warning_names_the_repository_it_writes(world: World) -> None:
    world.recipe("htop")
    repo = world.workspace / "meta-openembedded"
    other = world.workspace / "meta-other"
    for r in (repo, other):
        (r / "recipes").mkdir(parents=True, exist_ok=True)
    locs = [
        refresh.RecipeLocation(path=repo / "recipes" / "htop_1.0.bb", layer="meta-oe", repo_path=repo),
        refresh.RecipeLocation(path=other / "recipes" / "htop_1.0.bb", layer="meta-other", repo_path=other),
    ]

    result = refresh.run_refresh(
        world.req, builder=world.builder, locate_recipe=lambda _c, _r: locs, resolve_provider=lambda *_: None
    )

    assert "recorded meta-openembedded" in result.warnings[0]


@pytest.mark.parametrize("bad", ["soon", "-1", "nan"])
def test_malformed_lock_timeout_refuses_before_the_build(
    world: World, monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    world.recipe("htop")
    monkeypatch.setenv("BAKAR_FEED_LOCK_TIMEOUT", bad)

    with pytest.raises(refresh.RefreshFailedError, match=r"BAKAR_FEED_LOCK_TIMEOUT.*nothing was built"):
        world.run()

    assert world.build_calls == []


def test_recipe_in_one_layer_has_no_warning(world: World) -> None:
    world.recipe("htop")
    assert world.run().warnings == ()


def test_unreadable_pkgdata_refuses_and_keeps_the_previous_delta(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    world.recipe("htop")
    world.run()
    before = _delta_files(world)

    def deny(_path: Path) -> dict[str, str]:
        raise PermissionError("denied")

    monkeypatch.setattr(refresh, "_read_pkgdata", deny)

    with pytest.raises(refresh.RefreshFailedError, match="previous delta is untouched"):
        world.run()

    assert _delta_files(world) == before


def test_scratch_directory_failure_refuses_and_keeps_the_previous_delta(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.recipe("htop")
    world.run()
    before = _delta_files(world)
    assert before

    def full_disk(**_kw: object) -> str:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(refresh.tempfile, "mkdtemp", full_disk)

    with pytest.raises(refresh.RefreshFailedError, match="previous delta is untouched"):
        world.run()

    assert _delta_files(world) == before


_HOLDER = (
    "import fcntl, os, sys\n"
    "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT)\n"
    "fcntl.lockf(fd, fcntl.LOCK_EX)\n"
    "print('locked', flush=True)\n"
    "sys.stdin.read()\n"
)


@pytest.mark.parametrize("which", ["delta_root", "work_root"])
def test_refresh_does_not_swap_while_a_mirror_holds_the_root_lock(
    world: World, monkeypatch: pytest.MonkeyPatch, which: str
) -> None:
    import subprocess
    import sys

    world.recipe("htop")
    world.run()
    before = _delta_files(world)
    monkeypatch.setenv("BAKAR_FEED_LOCK_TIMEOUT", "0")
    root: Path = getattr(world.req, which)
    holders: list[subprocess.Popen[str]] = []

    def hold() -> None:
        proc = subprocess.Popen(
            [sys.executable, "-c", _HOLDER, str(root / ".bakar-feed.lock")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        holders.append(proc)
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "locked"

    world.during_build.append(hold)
    try:
        with pytest.raises(refresh.RefreshFailedError, match="locked by another bakar process"):
            world.run()
    finally:
        for proc in holders:
            assert proc.stdin is not None
            proc.stdin.close()
            proc.wait(timeout=10)

    assert _delta_files(world) == before


def test_locate_runs_once_per_recipe(world: World) -> None:
    world.recipe("htop")
    calls: list[str] = []
    locs = _two_layer_loc(world)[:1]

    def locate(_cfg: object, recipe: str) -> list[refresh.RecipeLocation]:
        calls.append(recipe)
        return locs

    refresh.run_refresh(world.req, builder=world.builder, locate_recipe=locate)  # type: ignore[arg-type]

    assert calls == ["htop"]


def _provider_locs(world: World, names: tuple[str, ...] = ("meta-first", "meta-oe")) -> list[refresh.RecipeLocation]:
    out = []
    for name in names:
        repo = world.workspace / name
        (repo / "recipes").mkdir(parents=True, exist_ok=True)
        out.append(refresh.RecipeLocation(path=repo / "recipes" / "htop_1.0.bb", layer=name, repo_path=repo))
    return out


def test_provider_resolved_to_the_second_layer_records_its_repository_and_commit(world: World) -> None:
    world.recipe("htop")
    locs = _provider_locs(world)
    seen: list[tuple[object, ...]] = []

    def resolver(cfg: object, kas_yaml: object, recipes: list[str]) -> dict[str, Path | None]:
        seen.append((cfg, kas_yaml, tuple(recipes)))
        return {"htop": locs[1].repo_path / "recipes" / "htop_1.0.bb"}

    result = refresh.run_refresh(
        world.req, builder=world.builder, locate_recipe=lambda _c, _r: locs, resolve_provider=resolver
    )

    (entry,) = result.entries
    assert (entry.layer, entry.layer_commit) == ("meta-oe", "abc123def456")
    assert seen == [(world.cfg, world.req.kas_yaml, ("htop",))]
    (warning,) = result.warnings
    assert "meta-first" in warning
    assert "meta-oe" in warning


def test_provider_resolved_through_a_symlinked_repository_still_matches(world: World, tmp_path: Path) -> None:
    world.recipe("htop")
    locs = _provider_locs(world)
    link = tmp_path / "link-to-meta-oe"
    link.symlink_to(locs[1].repo_path)
    locs[1] = refresh.RecipeLocation(path=locs[1].path, layer="meta-oe", repo_path=link)
    real_file = locs[1].repo_path.resolve() / "recipes" / "htop_1.0.bb"

    result = refresh.run_refresh(
        world.req,
        builder=world.builder,
        locate_recipe=lambda _c, _r: locs,
        resolve_provider=lambda *_: {"htop": real_file},
    )

    assert result.entries[0].layer == "link-to-meta-oe"


def test_provider_none_falls_back_to_the_first_layer_and_says_why(world: World) -> None:
    world.recipe("htop")
    locs = _provider_locs(world)

    result = refresh.run_refresh(
        world.req, builder=world.builder, locate_recipe=lambda _c, _r: locs, resolve_provider=lambda *_: None
    )

    (entry,) = result.entries
    assert (entry.layer, entry.layer_commit) == ("meta-first", None)
    (warning,) = result.warnings
    assert "span several repositories" in warning
    assert "bitbake could not say which built it" in warning


@pytest.mark.parametrize("answer", [{}, {"htop": None}, {"htop": Path("/nowhere/else/htop_1.0.bb")}])
def test_provider_empty_or_unmatched_answer_keeps_todays_fallback(world: World, answer: dict) -> None:
    world.recipe("htop")
    locs = _provider_locs(world)

    result = refresh.run_refresh(
        world.req, builder=world.builder, locate_recipe=lambda _c, _r: locs, resolve_provider=lambda *_: answer
    )

    (entry,) = result.entries
    assert (entry.layer, entry.layer_commit) == ("meta-first", None)


def test_provider_raising_falls_back_with_a_warning_and_the_refresh_succeeds(world: World) -> None:
    world.recipe("htop")
    locs = _provider_locs(world)

    def boom(*_a: object) -> dict:
        raise RuntimeError("kas shell exploded")

    result = refresh.run_refresh(
        world.req, builder=world.builder, locate_recipe=lambda _c, _r: locs, resolve_provider=boom
    )

    (entry,) = result.entries
    assert (entry.layer, entry.layer_commit) == ("meta-first", None)
    assert any("kas shell exploded" in w for w in result.warnings)
    assert result.channel_dir.is_dir()


def test_provider_single_layer_recipe_never_calls_the_resolver(world: World) -> None:
    world.recipe("htop")
    locs = _provider_locs(world, ("meta-oe",))
    calls: list[object] = []

    result = refresh.run_refresh(
        world.req,
        builder=world.builder,
        locate_recipe=lambda _c, _r: locs,
        resolve_provider=lambda *a: calls.append(a) or {},
    )

    assert calls == []
    assert result.warnings == ()


def test_provider_is_asked_once_for_only_the_ambiguous_recipes(world: World) -> None:
    world.recipe("htop")
    world.recipe("nano")
    req = refresh.RefreshRequest(
        **{
            **{f.name: getattr(world.req, f.name) for f in refresh.dataclasses.fields(world.req)},
            "recipes": ("htop", "nano"),
        }
    )
    two = _provider_locs(world)
    one = _provider_locs(world, ("meta-oe",))
    calls: list[tuple[str, ...]] = []

    def resolver(_c: object, _k: object, recipes: list[str]) -> dict:
        calls.append(tuple(recipes))
        return {}

    refresh.run_refresh(
        req,
        builder=world.builder,
        locate_recipe=lambda _c, r: two if r == "htop" else one,
        resolve_provider=resolver,
    )

    assert calls == [("htop",)]


def test_provider_same_repository_candidates_keep_the_known_commit(world: World) -> None:
    world.recipe("htop")
    repo = world.workspace / "meta-oe"
    (repo / "recipes").mkdir(parents=True, exist_ok=True)
    locs = [
        refresh.RecipeLocation(path=repo / "recipes" / "htop_1.0.bb", layer=name, repo_path=repo)
        for name in ("meta-oe", "meta-python")
    ]

    result = refresh.run_refresh(
        world.req, builder=world.builder, locate_recipe=lambda _c, _r: locs, resolve_provider=lambda *_: None
    )

    (entry,) = result.entries
    assert (entry.layer, entry.layer_commit) == ("meta-oe", "abc123def456")


def test_provider_shared_repository_with_unknown_commit_does_not_blame_ambiguity(world: World) -> None:
    world.recipe("htop")
    repo = world.workspace / "meta-unhashed"
    (repo / "recipes").mkdir(parents=True, exist_ok=True)
    locs = [
        refresh.RecipeLocation(path=repo / "recipes" / "htop_1.0.bb", layer=name, repo_path=repo)
        for name in ("meta-a", "meta-b")
    ]

    result = refresh.run_refresh(
        world.req, builder=world.builder, locate_recipe=lambda _c, _r: locs, resolve_provider=lambda *_: None
    )

    (entry,) = result.entries
    assert entry.layer_commit is None
    (warning,) = result.warnings
    assert "could not be determined" in warning
    assert "told apart" not in warning
    assert "could not say" not in warning


def test_provider_resolved_repository_without_a_known_commit_does_not_claim_ambiguity(world: World) -> None:
    world.recipe("htop")
    locs = _provider_locs(world, ("meta-first", "meta-unhashed"))

    result = refresh.run_refresh(
        world.req,
        builder=world.builder,
        locate_recipe=lambda _c, _r: locs,
        resolve_provider=lambda *_: {"htop": locs[1].path},
    )

    (entry,) = result.entries
    assert (entry.layer, entry.layer_commit) == ("meta-unhashed", None)
    (warning,) = result.warnings
    assert "could not be determined" in warning
    assert "meta-unhashed" in warning
    assert "could not say which built it" not in warning


def test_provider_of_prefers_the_innermost_of_nested_repositories(tmp_path: Path) -> None:
    outer, inner = tmp_path / "outer", tmp_path / "outer" / "nested"
    recipe = inner / "recipes" / "htop_1.0.bb"
    recipe.parent.mkdir(parents=True)
    outer_loc = refresh.RecipeLocation(path=outer / "x.bb", layer="meta-outer", repo_path=outer)
    inner_loc = refresh.RecipeLocation(path=recipe, layer="meta-inner", repo_path=inner)

    assert refresh._provider_of(recipe.resolve(), [outer_loc, inner_loc]) is inner_loc
    assert refresh._provider_of(recipe.resolve(), [inner_loc, outer_loc]) is inner_loc


@pytest.mark.parametrize("answer", [{}, {"htop": Path("/nowhere/else/htop_1.0.bb")}])
def test_unresolved_provider_warning_points_at_the_run_log(world: World, answer: dict) -> None:
    world.recipe("htop")
    locs = _provider_locs(world)

    result = refresh.run_refresh(
        world.req, builder=world.builder, locate_recipe=lambda _c, _r: locs, resolve_provider=lambda *_: answer
    )

    (warning,) = result.warnings
    assert warning.endswith("; see the recipe_files step of the build's run log (bakar log)")


def test_resolved_provider_warning_has_no_run_log_hint(world: World) -> None:
    world.recipe("htop")
    locs = _provider_locs(world)

    result = refresh.run_refresh(
        world.req,
        builder=world.builder,
        locate_recipe=lambda _c, _r: locs,
        resolve_provider=lambda *_: {"htop": locs[1].repo_path / "recipes" / "htop_1.0.bb"},
    )

    (warning,) = result.warnings
    assert "bakar log" not in warning
