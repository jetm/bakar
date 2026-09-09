"""Native/cross sstate seed: populate it, consume it, and tell when it is stale.

A seed is a copy of the native, cross and crosssdk sstate objects from a
completed build, kept somewhere a workspace wipe cannot reach. Pointing
``SSTATE_MIRRORS`` at it lets a later build restore the native toolchain
instead of rebuilding it.

The effect is the largest single one measured on this fleet: a
``core-image-minimal`` build on PC3 went 26.4 min to 9.0 min with the seed in
place, 65% of wall-clock, which is more than every other tuning lever in that
campaign combined. It was worth that much while living outside bakar entirely -
a personal script plus a hand-typed ``sstate_mirrors`` string in one user's
config - so a colleague building another target on another branch got none of
it and had no way to discover it existed.

Two properties decide the shape of everything below.

**The objects are target-independent.** ``sstate.bbclass`` prefixes every
native/cross/crosssdk object with ``${NATIVELSBSTRING}``, so they are selectable
by path structure rather than by recipe name, and one seed serves every MACHINE
and every image on a release. That is what makes seeding worth doing once rather
than per-target.

**The objects are release-dependent.** Native sstate hashes move with the
oe-core revision, so a scarthgap seed is inert for a wrynose build - it does not
corrupt anything, it simply never hits. The seed is therefore keyed by release
codename, using the same :func:`bakar.buildtools.resolve_oe_core_release_key`
that keeps buildtools toolchains apart for the same underlying reason.

Staleness is a silent performance cliff rather than a correctness bug: after a
pin bump the old objects stop matching and the build quietly rebuilds what it
used to restore, with nothing in the output saying so. :func:`read_seed_marker`
exists so a caller can say it out loud.
"""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from bakar.buildtools import resolve_oe_core_release_key

#: Filename substrings identifying a native/cross/crosssdk object that bitbake
#: keeps at the plain two-level hash path rather than under a
#: ``${NATIVELSBSTRING}`` prefix. ``do_populate_lic`` is the case that matters:
#: ``sstate.bbclass`` clears ``SSTATE_EXTRAPATH`` for that one task, so a native
#: recipe's license object lands beside the target ones and the prefix rule
#: below misses it.
FALLBACK_SUBSTRINGS = (
    "-native",
    "-cross-",
    "-cross-canadian-",
    "-crosssdk-",
    "nativesdk-",
)

#: Directory under the sstate root holding per-release seeds.
SEED_ROOT_NAME = ".native-seed"

#: Written into each seed so staleness is answerable without a build.
MARKER_NAME = ".bakar-seed.json"


@dataclass(frozen=True)
class SeedResult:
    """What a populate run copied."""

    files: int
    total_bytes: int
    dest: Path
    release_key: str | None
    source_missing: bool = False


@dataclass(frozen=True)
class SeedMarker:
    """The provenance record written beside a seed."""

    release_key: str | None
    source_dir: str
    files: int
    total_bytes: int
    created: float


def is_hash_prefix_dir(name: str) -> bool:
    """True for a plain two-hex-char sstate hash-prefix dir (e.g. ``7a``).

    Everything else at the sstate top level is a ``${NATIVELSBSTRING}`` prefix
    directory - ``universal/``, or the host's own LSB string such as
    ``cachyos/`` - which is exactly the set to take wholesale.
    """
    return len(name) == 2 and all(c in "0123456789abcdef" for c in name.lower())


def matches_fallback(filename: str) -> bool:
    """True for an object the prefix rule misses but that is still native/cross."""
    return any(sub in filename for sub in FALLBACK_SUBSTRINGS)


def seed_dir_for(sstate_dir: Path | str, release_key: str | None) -> Path:
    """Return the seed directory for *release_key* under *sstate_dir*.

    An unknown release gets its own ``_unknown`` bucket rather than sharing the
    root. Mixing releases in one directory is the failure this keying exists to
    prevent, and a workspace that cannot name its release is precisely the case
    where that would happen unnoticed.
    """
    return Path(sstate_dir) / SEED_ROOT_NAME / (release_key or "_unknown")


def seed_mirror_line(seed_dir: Path | str) -> str:
    """Return the ``SSTATE_MIRRORS`` value that consumes the seed at *seed_dir*.

    ``downloadfilename=PATH`` is required, not decorative: without it bitbake
    looks for a flat filename and misses every object, so the mirror reads as
    configured and never hits.
    """
    return f"file://.* file://{Path(seed_dir)}/PATH;downloadfilename=PATH"


def _copy_pair(src_file: Path, src_root: Path, dest_root: Path) -> list[int]:
    """Copy *src_file* and its ``.siginfo`` sidecar, preserving the relative path.

    The sidecar is copied with the object because sstate writes them as a pair
    and hash-equivalence lookups degrade without it - a seed that hits on the
    object but has no siginfo still costs a rebuild elsewhere.
    """
    sizes: list[int] = []
    dest_file = dest_root / src_file.relative_to(src_root)
    dest_file.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src_file, dest_file)
    sizes.append(dest_file.stat().st_size)

    siginfo_src = src_file.with_name(src_file.name + ".siginfo")
    if siginfo_src.is_file():
        siginfo_dest = dest_file.with_name(dest_file.name + ".siginfo")
        shutil.copy2(siginfo_src, siginfo_dest)
        sizes.append(siginfo_dest.stat().st_size)
    return sizes


def _copy_subtree(src_dir: Path, src_root: Path, dest_root: Path) -> list[int]:
    """Copy every file under a ``${NATIVELSBSTRING}`` prefix dir, path preserved."""
    sizes: list[int] = []
    for src_file in src_dir.rglob("*"):
        if not src_file.is_file():
            continue
        dest_file = dest_root / src_file.relative_to(src_root)
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_file, dest_file)
        sizes.append(dest_file.stat().st_size)
    return sizes


def _copy_fallback(hash_dir: Path, src_root: Path, dest_root: Path) -> list[int]:
    """Copy native/cross objects sitting in a plain hash-prefix dir."""
    sizes: list[int] = []
    for src_file in hash_dir.rglob("*"):
        if not src_file.is_file():
            continue
        if src_file.name.endswith(".siginfo"):
            continue  # taken as a sidecar of its own object, never on its own
        if matches_fallback(src_file.name):
            sizes.extend(_copy_pair(src_file, src_root, dest_root))
    return sizes


def populate_seed(
    source_dir: Path | str,
    dest_dir: Path | str,
    *,
    release_key: str | None = None,
) -> SeedResult:
    """Copy native/cross/crosssdk objects from *source_dir* into *dest_dir*.

    Overwrites on collision rather than skipping, so re-running after a pin bump
    refreshes stale objects instead of leaving them to shadow the new ones.

    A missing or empty source is reported through :attr:`SeedResult.source_missing`
    rather than raised. Populating from a workspace that has not built yet is an
    ordinary sequencing mistake, and a caller needs to tell it apart from a source
    that was read and genuinely held nothing native.
    """
    source = Path(source_dir)
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)

    if not source.is_dir():
        return SeedResult(0, 0, dest, release_key, source_missing=True)

    sizes: list[int] = []
    for entry in source.iterdir():
        if not entry.is_dir():
            continue
        if is_hash_prefix_dir(entry.name):
            sizes.extend(_copy_fallback(entry, source, dest))
        else:
            sizes.extend(_copy_subtree(entry, source, dest))

    result = SeedResult(len(sizes), sum(sizes), dest, release_key)
    write_seed_marker(dest, result, source)
    return result


def write_seed_marker(dest_dir: Path | str, result: SeedResult, source: Path) -> None:
    """Record what this seed was built from, so staleness is answerable later."""
    marker = {
        "release_key": result.release_key,
        "source_dir": str(source),
        "files": result.files,
        "total_bytes": result.total_bytes,
        "created": time.time(),
    }
    (Path(dest_dir) / MARKER_NAME).write_text(json.dumps(marker, indent=2))


def read_seed_marker(dest_dir: Path | str) -> SeedMarker | None:
    """Return the seed's provenance record, or None when there is none to read.

    None covers three states a caller may need to keep apart, and deliberately
    does not distinguish them here: no seed at all, a seed populated before this
    marker existed, and a marker that will not parse. All three mean the same
    thing to the only question this function is asked - the seed cannot say what
    it was built for.
    """
    path = Path(dest_dir) / MARKER_NAME
    try:
        data = json.loads(path.read_text())
    except OSError, ValueError:
        return None
    if not isinstance(data, dict):
        return None
    # TypeError belongs here beside ValueError, and only one of the two is
    # obvious: a marker holding `"created": {}` parses as JSON perfectly well
    # and then raises TypeError inside float(). Catching ValueError alone would
    # turn a corrupt marker - the exact case this function promises to report as
    # None - into a traceback out of a status command.
    try:
        return SeedMarker(
            release_key=data.get("release_key"),
            source_dir=str(data.get("source_dir", "")),
            files=int(data.get("files", 0)),
            total_bytes=int(data.get("total_bytes", 0)),
            created=float(data.get("created", 0.0)),
        )
    except TypeError, ValueError:
        return None


def resolve_seed_for_workspace(workspace: Path | str, sstate_dir: Path | str) -> tuple[Path, str | None]:
    """Return ``(seed_dir, release_key)`` for a workspace's oe-core release.

    Wraps the release lookup so callers do not each repeat the
    ``buildtools``-vs-``sstate`` reasoning about why the codename is the right
    key. A workspace kas has not cloned yet resolves to None, which
    :func:`seed_dir_for` buckets separately rather than silently sharing.
    """
    release_key = resolve_oe_core_release_key(Path(workspace))
    return seed_dir_for(sstate_dir, release_key), release_key
