"""Filtered hardlink view of an RPM deploy directory.

A leaf-recipe publish stages only the RPMs the named recipes produced, but the
staging script reads one deploy directory whole. This builds a sibling directory
holding hardlinks to the wanted RPMs, so the script sees a deploy tree that
contains only them.

Two things are kept regardless of the filter. ``avocado-repo.map`` is copied
because ``repo-stage-rpms.sh`` exits 1 when it is missing, and every arch
directory is recreated (empty if nothing matched) because the map names them and
the sync treats a missing arch directory differently from an empty one.
"""

from __future__ import annotations

import os
import shutil
from typing import TYPE_CHECKING

from bakar.feed_delta import parse_nevra

if TYPE_CHECKING:
    from pathlib import Path

REPO_MAP_NAME = "avocado-repo.map"


def _link_or_copy(src: Path, dst: Path) -> None:
    """Hardlink ``src`` to ``dst``, copying when the link is impossible.

    A copy is the fallback for a view on a different filesystem than the deploy
    dir, which a hardlink cannot cross.
    """
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def build_filtered_view(deploy_dir: Path, view_dir: Path, only_packages: set[str] | frozenset[str]) -> Path:
    """Populate ``view_dir`` with the RPMs in ``deploy_dir`` whose name is in ``only_packages``.

    Matching is on the NEVRA-parsed package name, never a prefix, so ``bar``
    does not select ``bar-lib``. A file whose name does not parse is skipped.
    Raises ``FileNotFoundError`` when ``deploy_dir`` has no repo map, since the
    staging script would fail on the view anyway.
    """
    repo_map = deploy_dir / REPO_MAP_NAME
    if not repo_map.is_file():
        raise FileNotFoundError(f"{repo_map} is missing; cannot build a filtered view")
    for arch_dir in sorted(p for p in deploy_dir.iterdir() if p.is_dir()):
        target = view_dir / arch_dir.name
        target.mkdir(parents=True, exist_ok=True)
        for rpm in sorted(arch_dir.glob("*.rpm")):
            try:
                name = parse_nevra(rpm.name).name
            except ValueError:
                continue
            if name in only_packages:
                _link_or_copy(rpm, target / rpm.name)
    view_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(repo_map, view_dir / REPO_MAP_NAME)
    return view_dir
