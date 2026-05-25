"""Layer git-hash collection.

Enumerates the repos backing the layers in a build's ``bblayers.conf``
and reports each repo's short git hash and current branch. Discovery
reuses :func:`bspctl.kas.parse_bblayers` rather than re-parsing the
bblayers file. Git invocations never raise: a repo whose checkout is
missing or whose ``git`` command fails is silently skipped (or, for the
branch query, reported with an empty branch - a valid detached HEAD).
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from bspctl.kas import parse_bblayers

if TYPE_CHECKING:
    from bspctl.config import BuildConfig


@dataclass
class LayerHash:
    repo: str
    short_hash: str
    branch: str  # empty string for a detached HEAD
    version: str | None = field(default=None)  # set for the bitbake entry


def _resolve_bblayers_paths(bblayers_conf: Path) -> dict[str, Path]:
    """Resolve TOPDIR-relative paths in bblayers.conf to {repo_name: git_root}.

    Used for generic BYO builds where BBLAYERS uses ``${TOPDIR}/../layers/``
    rather than the ``/work/sources/`` convention of NXP/TI container builds.
    Deduplicates by git root so multiple sublayers from one repo produce a
    single entry keyed on the repo directory basename.
    """
    build_dir = bblayers_conf.parent.parent  # build/conf/bblayers.conf -> build/
    topdir = str(build_dir)
    text = bblayers_conf.read_text()
    joined = " ".join(line.split("#", 1)[0] for line in text.splitlines()).replace("\\", " ")
    matches = re.findall(r'BBLAYERS\s*(?:\?\??|\+)?=\s*"([^"]*)"', joined)
    seen: set[Path] = set()
    result: dict[str, Path] = {}
    for body in matches:
        for token in body.split():
            token = token.strip().replace("${TOPDIR}", topdir)
            if not token:
                continue
            layer_path = Path(token).resolve()
            if not layer_path.is_dir():
                continue
            try:
                root_out = subprocess.run(
                    ["git", "-C", str(layer_path), "rev-parse", "--show-toplevel"],
                    capture_output=True,
                    text=True,
                )
            except OSError:
                continue
            if root_out.returncode != 0:
                continue
            git_root = Path(root_out.stdout.strip())
            if git_root in seen:
                continue
            seen.add(git_root)
            result[git_root.name] = git_root
    return result


def _find_bitbake_dir(cfg: BuildConfig, layer_roots: list[Path]) -> Path | None:
    """Locate the bitbake source directory.

    Checks ``cfg.bsp_bitbake_path`` first (NXP/TI builds), then looks for a
    ``bitbake/`` sibling of the parent directory of any discovered layer root
    (generic BYO builds where layers sit alongside a standalone bitbake repo).
    """
    candidate = cfg.bsp_bitbake_path
    if (candidate / "lib" / "bb" / "__init__.py").is_file():
        return candidate
    seen: set[Path] = set()
    for root in layer_roots:
        parent = root.parent
        if parent in seen:
            continue
        seen.add(parent)
        candidate = parent / "bitbake"
        if (candidate / "lib" / "bb" / "__init__.py").is_file():
            return candidate
    return None


def _read_bitbake_version(bitbake_dir: Path) -> str | None:
    """Extract ``__version__`` from ``bitbake/lib/bb/__init__.py``."""
    init_py = bitbake_dir / "lib" / "bb" / "__init__.py"
    try:
        text = init_py.read_text()
    except OSError:
        return None
    m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
    return m.group(1) if m else None


def _git_short_hash(path: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def _git_branch(path: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(path), "branch", "--show-current"],
            capture_output=True,
            text=True,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except OSError:
        return ""


def collect_layer_hashes(cfg: BuildConfig) -> list[LayerHash]:
    """Return a :class:`LayerHash` for each repo in ``bblayers.conf``.

    Returns ``[]`` when ``bblayers.conf`` does not exist (pre-first-build).
    The branch is an empty string when the repo is on a detached HEAD.
    Never raises on git failure. The result is sorted by repo name, with
    a ``bitbake`` entry appended last carrying the version read from
    ``lib/bb/__init__.py``.

    Supports two BBLAYERS path conventions:

    - NXP/TI container builds: ``/work/sources/<repo>/...`` paths where the
      repo's host path is ``cfg.bsp_root/sources/<repo>``.
    - Generic BYO builds: ``${TOPDIR}/../layers/<repo>/...`` paths resolved
      from the build directory via git root discovery.
    """
    if not cfg.bblayers_conf.is_file():
        return []

    # Strategy 1: /sources/ convention (NXP/TI container builds).
    repo_paths: dict[str, Path] = {}
    for repo in parse_bblayers(cfg.bblayers_conf):
        path = cfg.bsp_root / "sources" / repo
        if path.is_dir():
            repo_paths[repo] = path

    # Strategy 2: resolve TOPDIR-relative paths (generic/BYO builds).
    if not repo_paths:
        repo_paths = _resolve_bblayers_paths(cfg.bblayers_conf)

    results: list[LayerHash] = []
    for repo, path in repo_paths.items():
        short_hash = _git_short_hash(path)
        if short_hash is None:
            continue
        results.append(LayerHash(repo=repo, short_hash=short_hash, branch=_git_branch(path)))
    results.sort(key=lambda lh: lh.repo)

    # Append bitbake version entry when the source directory is locatable.
    bb_dir = _find_bitbake_dir(cfg, list(repo_paths.values()))
    if bb_dir is not None:
        short_hash = _git_short_hash(bb_dir)
        if short_hash is not None:
            results.append(
                LayerHash(
                    repo="bitbake",
                    short_hash=short_hash,
                    branch=_git_branch(bb_dir),
                    version=_read_bitbake_version(bb_dir),
                )
            )

    return results
