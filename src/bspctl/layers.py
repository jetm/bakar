"""Layer git-hash collection.

Enumerates the repos backing the layers in a build's ``bblayers.conf``
and reports each repo's short git hash and current branch. Discovery
reuses :func:`bspctl.kas.parse_bblayers` rather than re-parsing the
bblayers file. Git invocations never raise: a repo whose checkout is
missing or whose ``git`` command fails is silently skipped (or, for the
branch query, reported with an empty branch - a valid detached HEAD).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

from bspctl.kas import parse_bblayers

if TYPE_CHECKING:
    from bspctl.config import BuildConfig


@dataclass
class LayerHash:
    repo: str
    short_hash: str
    branch: str  # empty string for a detached HEAD


def collect_layer_hashes(cfg: BuildConfig) -> list[LayerHash]:
    """Return a :class:`LayerHash` for each repo in ``bblayers.conf``.

    Returns ``[]`` when ``bblayers.conf`` does not exist (pre-first-build).
    Skips repos whose ``sources/<repo>`` directory is absent or whose
    ``git rev-parse`` fails. The branch is an empty string when the repo
    is on a detached HEAD. Never raises on git failure. The result is
    sorted by repo name.
    """
    if not cfg.bblayers_conf.is_file():
        return []
    layers_map = parse_bblayers(cfg.bblayers_conf)
    results: list[LayerHash] = []
    for repo in layers_map:
        path = cfg.bsp_root / "sources" / repo
        if not path.is_dir():
            continue
        try:
            rev = subprocess.run(
                ["git", "-C", str(path), "rev-parse", "--short", "HEAD"],
                capture_output=True,
                text=True,
            )
        except OSError:
            continue
        if rev.returncode != 0:
            continue
        short_hash = rev.stdout.strip()
        try:
            branch_out = subprocess.run(
                ["git", "-C", str(path), "branch", "--show-current"],
                capture_output=True,
                text=True,
            )
            branch = branch_out.stdout.strip() if branch_out.returncode == 0 else ""
        except OSError:
            branch = ""
        results.append(LayerHash(repo=repo, short_hash=short_hash, branch=branch))
    return sorted(results, key=lambda lh: lh.repo)
