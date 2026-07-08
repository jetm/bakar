"""Unified pin-state reading across the three bakar workspace families.

Pins are expressed differently per family:

- **NXP/TI**: a SHA-pinned ``repo``/``oe-layertool`` manifest XML. Pins are
  read via :func:`bakar.workspace.parse_manifest_pins`.
- **BYO/bbsetup**: a kas lockfile (``kas lock --format json`` output) whose
  ``repos.<name>.commit`` fields carry the pinned SHA. When no lockfile is
  present, the pin falls back to each cloned source's current git ``HEAD``.

This module keeps the family branch in one tested place so ``drift`` and
``changelog`` stay thin wrappers over :func:`read_pins`. Commit distances reuse
the best-effort ``git rev-list --count`` logic already in
:mod:`bakar.manifest_diff`.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from bakar.gitutil import run_git
from bakar.manifest_diff import _rev_list_count
from bakar.workspace import parse_manifest_pins

if TYPE_CHECKING:
    from pathlib import Path

# Families whose pins live in a manifest XML; everything else reads a kas
# lockfile (with a git-HEAD fallback).
_MANIFEST_FAMILIES = frozenset({"nxp", "ti"})

# Subdirectories scanned for cloned source repos, mirroring
# ``layers.discover_source_repos``.
_SOURCE_ROOTS = ("sources", "layers")


def parse_kas_lockfile(path: Path) -> dict[str, str]:
    """Return ``{<repo name>: <commit sha>}`` from a kas lockfile JSON.

    The lockfile shape is ``{"repos": {<name>: {"commit": <sha>}}}`` (the
    output of ``kas lock --format json``). Repos without a ``commit`` field
    are skipped.

    Raises:
        ValueError: when the file cannot be read, is not valid JSON, or lacks
            a top-level ``repos`` key.
    """
    try:
        raw = json.loads(path.read_text())
    except OSError as exc:
        raise ValueError(f"cannot read kas lockfile {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in kas lockfile {path}: {exc}") from exc

    if not isinstance(raw, dict) or "repos" not in raw:
        raise ValueError(
            f"kas lockfile {path} has no top-level 'repos' key; expected {{'repos': {{<name>: {{'commit': <sha>}}}}}}"
        )

    pins: dict[str, str] = {}
    repos = raw["repos"]
    if isinstance(repos, dict):
        for name, entry in repos.items():
            if isinstance(entry, dict):
                commit = entry.get("commit")
                if isinstance(commit, str) and commit:
                    pins[name] = commit
    return pins


def _git_head(checkout: Path) -> str | None:
    """Return the resolved ``HEAD`` SHA of a checkout, or None on failure."""
    if not checkout.is_dir():
        return None
    out = run_git(["git", "-C", str(checkout), "rev-parse", "HEAD"])
    if out is None or out.returncode != 0:
        return None
    sha = out.stdout.strip()
    return sha or None


def _git_head_pins(workspace: Path) -> dict[str, str]:
    """Return ``{<repo name>: <HEAD sha>}`` for every cloned source repo.

    Scans ``workspace/sources`` then ``workspace/layers`` for immediate
    subdirectories that are git repos, mirroring
    :func:`bakar.layers.discover_source_repos`. Never raises: an unreadable
    directory or a repo whose ``HEAD`` cannot be resolved is skipped.
    """
    pins: dict[str, str] = {}
    for root_name in _SOURCE_ROOTS:
        root = workspace / root_name
        if not root.is_dir():
            continue
        try:
            entries = list(root.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.name in pins:
                continue
            if not entry.is_dir() or not (entry / ".git").exists():
                continue
            sha = _git_head(entry)
            if sha is not None:
                pins[entry.name] = sha
    return pins


def _strip_path_prefix(key: str) -> str:
    """Return the bare repo name from a manifest pin key.

    Manifest pins use keys like ``"sources/meta-imx"``; stripping the leading
    path component gives the bare name that matches the checkout directory
    under ``sources/`` or ``layers/``.
    """
    return key.split("/", 1)[-1]


def _normalize_pin_keys(pins: dict[str, str], *, is_manifest: bool) -> dict[str, str]:
    """Return ``{bare_name: sha}`` from a raw pins dict.

    Manifest pins carry a leading path component (``"sources/meta-imx"``); the
    bare name is the last path component. Lockfile and git-HEAD pins already use
    bare names, so they pass through unchanged.
    """
    if is_manifest:
        return {_strip_path_prefix(k): v for k, v in pins.items()}
    return dict(pins)


def read_pins(
    family: str,
    *,
    manifest: Path | None = None,
    lockfile: Path | None = None,
    workspace: Path | None = None,
) -> dict[str, str]:
    """Return ``{<source>: <pinned sha>}`` for a workspace family.

    NXP/TI families read pins from the ``manifest`` XML via
    :func:`bakar.workspace.parse_manifest_pins`. BYO/bbsetup families read the
    kas ``lockfile`` when present, falling back to each cloned source's git
    ``HEAD`` under ``workspace``.

    Args:
        family: one of ``nxp``, ``ti``, ``bbsetup``, ``generic``.
        manifest: manifest XML path (NXP/TI).
        lockfile: kas lockfile JSON path (BYO/bbsetup).
        workspace: workspace root for the git-HEAD fallback (BYO/bbsetup).

    Raises:
        ValueError: when a manifest family is requested without a manifest, or
            a lockfile family is requested with neither a lockfile nor a
            workspace.
    """
    if family in _MANIFEST_FAMILIES:
        if manifest is None:
            raise ValueError(f"family {family!r} requires a manifest path")
        return dict(parse_manifest_pins(manifest))

    if lockfile is not None and lockfile.is_file():
        return parse_kas_lockfile(lockfile)

    if workspace is not None:
        return _git_head_pins(workspace)

    raise ValueError(f"family {family!r} requires a kas lockfile or a workspace for the git-HEAD fallback")


def commit_distance(checkout: Path, old_sha: str, new_sha: str) -> int | None:
    """Return the commit count of ``old_sha..new_sha`` in a checkout, or None.

    Best-effort: a missing checkout, a non-git directory, a failed ``git``
    command, or unparseable output all yield ``None``. Reuses the
    ``git rev-list --count`` logic in :mod:`bakar.manifest_diff`.
    """
    return _rev_list_count(checkout, old_sha, new_sha)
