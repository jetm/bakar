"""Per-layer SHA diff between two pinned manifests.

Compares two SHA-pinned ``repo`` manifest XMLs (as produced by ``bakar
lock`` / ``repo manifest -r``) and reports, per layer, the old and new
SHA plus a best-effort commit count between them. The pin parsing reuses
:func:`bakar.workspace.parse_manifest_pins`; the commit count is read
from ``git rev-list`` only when the layer checkout is present locally and
both SHAs are known. Git invocations never raise: a missing checkout or a
failed ``git`` command yields ``commit_count=None`` rather than an error.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

from bakar.workspace import parse_manifest_pins

if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class LayerDiff:
    layer: str
    old_sha: str | None
    new_sha: str | None
    commit_count: int | None


def _rev_list_count(checkout: Path, old: str, new: str) -> int | None:
    """Return the commit count of ``old..new`` in a checkout, or None.

    Best-effort: a missing checkout, a non-git directory, a failed
    ``git`` command, or unparseable output all yield ``None``.
    """
    if not checkout.is_dir():
        return None
    try:
        out = subprocess.run(
            ["git", "-C", str(checkout), "rev-list", "--count", f"{old}..{new}"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if out.returncode != 0:
        return None
    try:
        return int(out.stdout.strip())
    except ValueError:
        return None


def diff_manifests(old_path: Path, new_path: Path, *, checkout_root: Path | None = None) -> list[LayerDiff]:
    """Return a :class:`LayerDiff` per layer across two pinned manifests.

    Layers are the union of paths pinned in either manifest, sorted by
    name. ``old_sha``/``new_sha`` come from each manifest's pin map and
    are ``None`` when the layer is absent on that side. ``commit_count``
    is ``0`` for an unchanged layer; otherwise it is the best-effort
    ``git rev-list --count <old>..<new>`` against ``checkout_root/layer``
    when both SHAs are present and the checkout exists, and ``None``
    otherwise (one side missing, no checkout root, or git failure).
    """
    old_pins = dict(parse_manifest_pins(old_path))
    new_pins = dict(parse_manifest_pins(new_path))

    diffs: list[LayerDiff] = []
    for layer in sorted(old_pins.keys() | new_pins.keys()):
        old_sha = old_pins.get(layer)
        new_sha = new_pins.get(layer)

        if old_sha == new_sha:
            commit_count: int | None = 0
        elif old_sha is not None and new_sha is not None and checkout_root is not None:
            commit_count = _rev_list_count(checkout_root / layer, old_sha, new_sha)
        else:
            commit_count = None

        diffs.append(LayerDiff(layer=layer, old_sha=old_sha, new_sha=new_sha, commit_count=commit_count))

    return diffs
