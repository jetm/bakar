"""Delta channel writers: ``recipe-map.json`` and ``delta.json``.

Pure writers. Every value the writer cannot derive from its arguments is
supplied by the caller: layer commits, the base release and channel, the run id
and the delta path. Nothing here reads the project's avocado configuration or
shells out.

The ``pr`` field is the RPM release as it appears in the package file name
(``PKGR`` in the runtime pkgdata, e.g. ``r0.1`` with the PR-server suffix), not
the recipe's ``PR``. ``pv`` and ``pr`` are therefore parsed from the file name,
while ``epoch`` is passed in (``PKGE``, 0 when unset) because an RPM file name
carries no epoch.

``task_hash`` is always written, as ``null`` when the bitbake taskhash is
unavailable; the comparison key then falls back to
``epoch, pv, pr, srcrev, layer_commit``.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

SCHEMA_VERSION = 1
RECIPE_MAP_NAME = "recipe-map.json"
DELTA_NAME = "delta.json"


@dataclass(frozen=True)
class Nevra:
    """Name, version, release and arch of an RPM; the epoch is not in the file name."""

    name: str
    version: str
    release: str
    arch: str


def parse_nevra(filename: str) -> Nevra:
    """Parse ``name-version-release.arch[.rpm]`` into its four fields.

    The arch is split off at the last dot first, because the release itself may
    contain dots (``r0.1``). Names may contain dashes; version and release may
    not.
    """
    stem = Path(filename).name
    stem = stem.removesuffix(".rpm")
    body, dot, arch = stem.rpartition(".")
    if not dot or not arch or not body:
        raise ValueError(f"not an RPM file name (no arch): {filename!r}")
    rest, dash, release = body.rpartition("-")
    if not dash or not release or not rest:
        raise ValueError(f"not an RPM file name (no release): {filename!r}")
    name, dash, version = rest.rpartition("-")
    if not dash or not version or not name:
        raise ValueError(f"not an RPM file name (no version): {filename!r}")
    return Nevra(name=name, version=version, release=release, arch=arch)


@dataclass(frozen=True)
class RecipeEntry:
    recipe: str
    packages: list[str]
    layer: str
    epoch: int
    pv: str
    pr: str
    srcrev: str | None
    layer_commit: str
    machine: str
    release: str
    task_hash: str | None

    def delta_key(self) -> tuple[str, ...]:
        """The identity compared between two builds of the same recipe."""
        if self.task_hash is not None:
            return ("task_hash", self.task_hash)
        return (
            str(self.epoch),
            self.pv,
            self.pr,
            self.srcrev or "",
            self.layer_commit,
        )


def entry_from_package_files(  # noqa: PLR0913 - keyword-only fields of one record
    *,
    recipe: str,
    package_files: list[str],
    packages: list[str],
    layer: str,
    epoch: int,
    srcrev: str | None,
    layer_commit: str,
    machine: str,
    release: str,
    task_hash: str | None,
) -> RecipeEntry:
    """Build an entry, taking ``pv`` and ``pr`` from the recipe's package file names."""
    if not package_files:
        raise ValueError(f"recipe {recipe!r} has no package files")
    parsed = [parse_nevra(f) for f in package_files]
    versions = {(p.version, p.release) for p in parsed}
    if len(versions) != 1:
        raise ValueError(f"recipe {recipe!r} package files disagree on version-release: {sorted(versions)}")
    pv, pr = next(iter(versions))
    return RecipeEntry(
        recipe=recipe,
        packages=list(packages),
        layer=layer,
        epoch=epoch,
        pv=pv,
        pr=pr,
        srcrev=srcrev,
        layer_commit=layer_commit,
        machine=machine,
        release=release,
        task_hash=task_hash,
    )


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_recipe_map(channel_dir: Path, entries: list[RecipeEntry]) -> Path:
    """Write ``recipe-map.json`` in the delta channel directory."""
    payload = {
        "schema_version": SCHEMA_VERSION,
        "entries": [asdict(e) for e in entries],
    }
    path = channel_dir / RECIPE_MAP_NAME
    _write_json_atomic(path, payload)
    return path


def write_delta(  # noqa: PLR0913 - keyword-only fields of one record
    channel_dir: Path,
    *,
    base_release: str,
    base_channel: str,
    built_recipes: list[str],
    bakar_run_id: str,
    delta_path: str,
    snapshot_id: str | None = None,
) -> Path:
    """Write ``delta.json`` in the delta channel directory."""
    payload = {
        "schema_version": SCHEMA_VERSION,
        "base": {
            "release": base_release,
            "channel": base_channel,
            "snapshot_id": snapshot_id,
        },
        "built_recipes": list(built_recipes),
        "bakar_run_id": bakar_run_id,
        "delta_path": delta_path,
    }
    path = channel_dir / DELTA_NAME
    _write_json_atomic(path, payload)
    return path
