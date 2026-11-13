"""A fake host build tree for the feed refresh tests.

This is a test helper module, not a test module: it holds no test functions.
Import what a test needs directly::

    from tests._feed_world import HASH, World, make_rpm

``World`` builds a tmpdir with deploy, pkgdata and stamps, a ``RefreshRequest``
and call records. The ``world`` fixture that wires its monkeypatches stays in
``tests/test_feed_refresh_pipeline.py``.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import TYPE_CHECKING

from bakar.commands import _feed_refresh as refresh

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

HASH = "a" * 64


def make_rpm(  # noqa: PLR0913
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
            make_rpm(self.deploy, "core2_64", rpm_name, version=version, mtime=2_000_000)
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
