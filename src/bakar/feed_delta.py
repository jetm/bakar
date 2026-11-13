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
import re
import tempfile
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
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
    layer_commit: str | None
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
            self.layer_commit or "",
        )


# Stamp tasks that carry the recipe's taskhash, best signal first. The RPM
# write task is the one that produced the published package; ``do_package`` is
# the fallback when neither RPM stamp exists.
_STAMP_TASKS = ("do_package_write_rpm", "do_package_write_rpm_setscene", "do_package")
_HASH_RE = re.compile(r"[0-9a-f]{64}")


def _pv_candidates(pkgv: str) -> list[str]:
    """Stamp PV names to try for a PKGV, most specific first.

    Stamps are named by ``${PV}`` but the caller holds the PKGV parsed from the
    RPM file name. They agree for release recipes; a ``+git`` recipe's PKGV is
    ``<base>+git0+<srcrev>`` while its PV is ``<base>+git``.
    """
    candidates = [pkgv]
    if "+git" in pkgv:
        candidates.append(pkgv.split("+git", 1)[0] + "+git")
    if "+" in pkgv:
        candidates.append(pkgv.split("+", 1)[0])
    return list(dict.fromkeys(candidates))


def _read_for_pv(stamps_dir: Path, recipe: str, pv: str) -> tuple[bool, str | None]:
    """Return (any stamp matched, the unambiguous hash or None) for one PV."""
    found: dict[str, set[str]] = {task: set() for task in _STAMP_TASKS}
    for recipe_dir in stamps_dir.glob(f"*/{recipe}"):
        if not recipe_dir.is_dir():
            continue
        for stamp in recipe_dir.iterdir():
            for task in _STAMP_TASKS:
                prefix = f"{pv}.{task}."
                if stamp.name.startswith(prefix) and _HASH_RE.fullmatch(stamp.name[len(prefix) :]):
                    found[task].add(stamp.name[len(prefix) :])
    for task in _STAMP_TASKS:
        if found[task]:
            hashes = found[task]
            return True, next(iter(hashes)) if len(hashes) == 1 else None
    return False, None


def read_task_hash(stamps_dir: Path, recipe: str, pv: str) -> str | None:
    """Read a recipe's bitbake taskhash from its stamp file names.

    Stamps live at ``<stamps_dir>/<pkgarch>/<recipe>/<pv>.<task>.<taskhash>``.
    ``pv`` is the version parsed from the RPM file name (the PKGV), so the
    stamp PV candidates derived from it are tried in order; the first candidate
    with any matching stamp decides. Within it, the first task in
    ``_STAMP_TASKS`` with a stamp decides; if its stamps carry more than one
    distinct hash the answer is ambiguous and nothing is returned.
    Returns None when no stamp matches, so the caller keeps its explicit null
    ``task_hash``. A hash is never invented; ``.sigdata.`` twins are not stamps.
    """
    if not stamps_dir.is_dir():
        return None
    for candidate in _pv_candidates(pv):
        matched, task_hash = _read_for_pv(stamps_dir, recipe, candidate)
        if matched:
            return task_hash
    return None


def entry_from_package_files(  # noqa: PLR0913 - keyword-only fields of one record
    *,
    recipe: str,
    package_files: list[str],
    packages: list[str],
    layer: str,
    epoch: int,
    srcrev: str | None,
    layer_commit: str | None,
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


_SOURCE_KEYS = ("host", "run_id", "pulled_at", "observed_layers", "pin_override")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")


def _bad_source(key: str, want: str, got: object) -> ValueError:
    return ValueError(f"source {key!r} must be {want}, got {got!r}")


def _is_commit_or_none(value: object) -> bool:
    return value is None or (isinstance(value, str) and _COMMIT_RE.fullmatch(value) is not None)


def _validate_source(source: Mapping[str, object]) -> None:
    """Reject a malformed source record before anything is written."""
    for key in _SOURCE_KEYS:
        if key not in source:
            raise ValueError(f"source is missing {key!r}")
    extra = sorted(str(k) for k in source if k not in _SOURCE_KEYS)
    if extra:
        raise ValueError(f"source has unknown key(s): {', '.join(extra)}")
    for key in ("host", "run_id"):
        value = source[key]
        if not isinstance(value, str) or not value:
            raise _bad_source(key, "a non-empty string", value)
    pulled_at = source["pulled_at"]
    try:
        parsed = datetime.fromisoformat(pulled_at) if isinstance(pulled_at, str) else None
    except ValueError:
        parsed = None
    if parsed is None or parsed.utcoffset() != timedelta(0):
        raise _bad_source("pulled_at", "a UTC ISO-8601 string", pulled_at)
    layers = source["observed_layers"]
    if not isinstance(layers, Mapping):
        raise _bad_source("observed_layers", "a mapping", layers)
    for name, commit in layers.items():
        if not isinstance(name, str) or not _is_commit_or_none(commit):
            raise _bad_source("observed_layers", "a mapping of name to 40-hex commit or None", {name: commit})
    if not isinstance(source["pin_override"], bool):
        raise _bad_source("pin_override", "a bool", source["pin_override"])


def write_delta(  # noqa: PLR0913 - keyword-only fields of one record
    channel_dir: Path,
    *,
    base_release: str,
    base_channel: str,
    built_recipes: list[str],
    bakar_run_id: str,
    delta_path: str,
    snapshot_id: str | None = None,
    source: Mapping[str, object] | None = None,
) -> Path:
    """Write ``delta.json`` in the delta channel directory.

    ``source`` is recorded only for a refresh from another host; a local refresh
    passes nothing and the file carries no ``source`` key at all.
    """
    if source is not None:
        _validate_source(source)
    payload: dict[str, object] = {
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
    if source is not None:
        payload["source"] = {key: source[key] for key in _SOURCE_KEYS}
    path = channel_dir / DELTA_NAME
    _write_json_atomic(path, payload)
    return path
