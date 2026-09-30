"""Ledger of native/cross task signatures captured from finished builds.

bitbake keeps only the newest ``*.sigdata.<hash>`` per task under ``stamps/``,
so once a native or cross recipe is rebuilt the previous signature is gone and
a later ``bakar diffsigs`` cannot say what changed.  This module copies each
executed native/cross task's signature file into a bakar-owned ledger next to
the sstate cache, records a per-run manifest, and provides the lookups that
attribution needs.

Foundation module: stdlib only.  It owns every on-disk layout rule for
capture and lookup and never raises ``OSError`` out of a public function.

Ledger layout: ``<sstate_dir>/.bakar/native-sigdata/<recipe>/<task>.<hash>.sigdata``.
"""

from __future__ import annotations

import glob
import json
import os
import re
import shutil
import stat
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

_Path = Path
LEDGER_SUBDIR = Path(".bakar") / "native-sigdata"
MANIFEST_NAME = "native-signatures.json"
DEFAULT_SLACK_SECONDS = 120.0

_RECIPE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+._-]*$")
_TASK_RE = re.compile(r"^do_[A-Za-z0-9_]+$")
_HASH_RE = re.compile(r"^[0-9a-f]{40,128}$")
_SIGINFO_TAIL_RE = re.compile(r"^([0-9a-f]{40,128})_([A-Za-z0-9_]+)\.tar\.[^.]+\.siginfo$")


def valid_recipe(name: object) -> bool:
    return isinstance(name, str) and ".." not in name and _RECIPE_RE.match(name) is not None


def valid_task(name: object) -> bool:
    return isinstance(name, str) and _TASK_RE.match(name) is not None


def valid_hash(value: object) -> bool:
    return isinstance(value, str) and _HASH_RE.match(value) is not None


# A normalized event row names its recipe by PF ("quilt-native-0.69-r0"), while the
# stamps directories, the ledger and sstate siginfo names are all keyed by PN. Same
# pattern as task_timings.strip_recipe_version, kept here so this module stays stdlib-only.
_PF_VERSION_RE = re.compile(r"-\d[^-]*(?:-r\d+)?$")


def row_pn(row: Mapping[str, Any]) -> str:
    """The PN of an event row's recipe (``quilt-native-0.69-r0`` -> ``quilt-native``)."""
    pf = str(row.get("recipe") or "")
    return _PF_VERSION_RE.sub("", pf) or pf


def is_native_or_cross(recipe: str) -> bool:
    """True for ``*-native``, ``*-cross-*`` and ``*-crosssdk-*`` (not ``-cross-canadian-``)."""
    if not isinstance(recipe, str):
        return False
    if recipe.endswith("-native"):
        return True
    if "-cross-canadian-" in recipe:
        return False
    return "-cross-" in recipe or "-crosssdk-" in recipe


def _native_rows(artifact: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    rows = artifact.get("tasks") or []
    return [r for r in rows if isinstance(r, dict) and is_native_or_cross(row_pn(r))]


def executed_native_tasks(artifact: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Native/cross rows that actually ran (not setscene) and succeeded or failed."""
    return [
        r
        for r in _native_rows(artifact)
        if not str(r.get("task") or "").endswith("_setscene") and r.get("outcome") in ("succeeded", "failed")
    ]


def restored_native_count(artifact: Mapping[str, Any]) -> int:
    """Native/cross setscene rows restored from sstate."""
    return sum(
        1
        for r in _native_rows(artifact)
        if str(r.get("task") or "").endswith("_setscene") and r.get("outcome") == "succeeded"
    )


def stamp_roots(resolved_tmpdir: Path, topdir: Path) -> list[Path]:
    """``<tmpdir>/stamps`` plus each existing ``<topdir>/tmp-*/stamps`` (multiconfig)."""
    candidates: list[Path] = [_Path(resolved_tmpdir) / "stamps"]
    try:
        for extra in sorted(glob.glob(os.path.join(glob.escape(str(topdir)), "tmp-*", "stamps"))):
            p = _Path(extra)
            if p.is_dir():
                candidates.append(p)
    except OSError:
        pass
    seen: set[Path] = set()
    out: list[Path] = []
    for c in candidates:
        try:
            key = c.resolve()
        except OSError:
            key = c
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


def _as_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except TypeError, ValueError:
        return None


def _sigdata_hash(path: _Path, task: str) -> str | None:
    marker = f".{task}.sigdata."
    name = path.name
    idx = name.rfind(marker)
    if idx < 0:
        return None
    h = name[idx + len(marker) :]
    return h if valid_hash(h) else None


def _sigdata_glob(root: _Path, recipe: str, task: str, hash_: str = "*") -> list[_Path]:
    pattern = os.path.join(glob.escape(str(root)), "*", glob.escape(recipe), f"*.{glob.escape(task)}.sigdata.{hash_}")
    try:
        return [_Path(p) for p in glob.glob(pattern)]
    except OSError:
        return []


def find_task_sigdata(
    roots: Iterable[Path],
    ref: Mapping[str, Any],
    *,
    slack_seconds: float = DEFAULT_SLACK_SECONDS,
    not_before: float | None = None,
) -> Path | None:
    """Newest signature file inside the task's own ``[started-slack, completed+slack]`` window.

    ``ref`` is a normalized task row.  A row with a missing ``started`` or
    ``completed`` is not searched.  ``not_before`` is the run's own build start:
    the slack never reaches before it, so a task that failed before writing a new
    signature cannot pick up the file the previous build left a minute earlier.
    """
    recipe, task = row_pn(ref), ref.get("task")
    if not (valid_recipe(recipe) and valid_task(task)):
        return None
    started, completed = _as_float(ref.get("started")), _as_float(ref.get("completed"))
    if started is None or completed is None:
        return None
    lo, hi = started - slack_seconds, completed + slack_seconds
    if not_before is not None:
        lo = max(lo, not_before)
    best: tuple[float, _Path] | None = None
    for root in roots:
        # The root itself may be a symlink (a TMPDIR on another disk), so containment is judged
        # against its resolved path; a symlinked arch or recipe directory below it must not leave it.
        root_real = os.path.realpath(root)
        for path in _sigdata_glob(_Path(root), str(recipe), str(task)):
            if _sigdata_hash(path, str(task)) is None:
                continue
            try:
                st = path.lstat()
            except OSError:
                continue
            # A symlink would let a planted stamps entry copy any readable file into the shared ledger.
            if not stat.S_ISREG(st.st_mode):
                continue
            if os.path.commonpath([root_real, os.path.realpath(path)]) != root_real:
                continue
            mtime = st.st_mtime
            if lo <= mtime <= hi and (best is None or mtime > best[0]):
                best = (mtime, path)
    return best[1] if best else None


@dataclass
class CaptureResult:
    executed: int = 0
    restored: int = 0
    copied: int = 0  # entries now in the ledger, including ones already present from an earlier capture
    missing: int = 0
    invalid: int = 0
    failed: int = 0
    manifest_failed: bool = False  # the run manifest could not be written, so insights cannot attribute this run


def ledger_root(sstate_dir: Path) -> Path:
    return _Path(sstate_dir) / LEDGER_SUBDIR


_SEEN_SUFFIX = ".seen"
_SEEN_TAIL_BYTES = 4096  # about 200 sightings; older ones are never the newest before any recent run


def _mark_seen(dest: _Path) -> None:
    """Append this moment to ``dest``'s ``.seen`` marker, leaving the entry itself untouched.

    The marker lists every recapture time, one per line.  Predecessors are ordered by
    the last sighting before the analysed run, so a signature built, replaced and built
    again looks newer than its replacement, and the run that recaptured it does not
    hide the sighting before it.  A failure here only costs ordering accuracy.
    """
    marker = dest.with_name(dest.name + _SEEN_SUFFIX)
    try:
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o644)
    except OSError:
        return
    try:
        os.write(fd, f"{time.time():.3f}\n".encode())
    except OSError:
        pass
    finally:
        os.close(fd)


def _read_sightings(marker: str) -> list[float]:
    """Times recorded in a ``.seen`` marker (newest ones only); unreadable or odd lines are skipped."""
    try:
        with open(marker, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - _SEEN_TAIL_BYTES))
            text = fh.read().decode("ascii", "ignore")
    except OSError:
        return []
    out: list[float] = []
    for line in text.splitlines():
        try:
            out.append(float(line))
        except ValueError:
            continue
    return out


def _copy_into_ledger(src: _Path, dest: _Path) -> None:
    """Copy ``src`` to ``dest`` atomically; the entry takes the first-capture time as mtime.

    An entry already present is left untouched (content and mtime): rewriting it
    would move its mtime past a later run's start, and ``earlier_ledger_signatures``
    would then drop the signature that run rebuilt.  Its ``.seen`` marker is
    refreshed instead, so the recapture still orders correctly.
    """
    if dest.exists():
        _mark_seen(dest)
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=".", suffix=".part")
    os.close(fd)
    tmp = _Path(tmp_name)
    try:
        shutil.copyfile(src, tmp)
        os.chmod(tmp, 0o644)
        os.replace(tmp, dest)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def capture_run(
    artifact: Mapping[str, Any], *, roots: Iterable[Path], sstate_dir: Path, run_dir: Path
) -> CaptureResult:
    """Copy executed native/cross task signatures into the ledger and write the run manifest.

    A signature already in the ledger is not rewritten, so an entry keeps its
    first-capture mtime; it still counts in ``copied`` and is listed in the manifest.
    """
    roots = list(roots)
    result = CaptureResult(restored=restored_native_count(artifact))
    rows = executed_native_tasks(artifact)
    result.executed = len(rows)
    ledger = ledger_root(sstate_dir)
    manifest: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    build = artifact.get("build")
    build_start = _as_float(build.get("started")) if isinstance(build, dict) else None
    for row in rows:
        recipe, task = row_pn(row), row.get("task")
        if not (valid_recipe(recipe) and valid_task(task)):
            result.invalid += 1
            continue
        src = find_task_sigdata(roots, row, not_before=build_start)
        if src is None:
            result.missing += 1
            continue
        hash_ = _sigdata_hash(src, str(task))
        if hash_ is None:
            result.invalid += 1
            continue
        dest = ledger / str(recipe) / f"{task}.{hash_}.sigdata"
        try:
            _copy_into_ledger(src, dest)
        except OSError:
            result.failed += 1
            continue
        result.copied += 1
        key = (str(recipe), str(task), hash_)
        if key not in seen:
            seen.add(key)
            manifest.append({"recipe": key[0], "task": key[1], "hash": key[2]})
    try:
        run = _Path(run_dir)
        run.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=run, prefix=".", suffix=".part")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"schema": 1, "tasks": manifest}, indent=2) + "\n")
        os.replace(tmp_name, run / MANIFEST_NAME)
    except OSError:
        result.manifest_failed = True
    return result


def effective_sstate_dir(cfg_value: str | Path | None) -> Path | None:
    """``$SSTATE_DIR`` if set, else the config value, absolutized; None when both empty."""
    raw = os.environ.get("SSTATE_DIR") or (str(cfg_value) if cfg_value else "")
    if not raw:
        return None
    return _Path(os.path.abspath(os.path.expanduser(raw)))


def ledger_entry(sstate_dir: Path, recipe: str, task: str, hash_: str) -> Path | None:
    """Direct ledger path for a signature, or None when absent or any part is invalid."""
    if not (valid_recipe(recipe) and valid_task(task) and valid_hash(hash_)):
        return None
    p = ledger_root(sstate_dir) / recipe / f"{task}.{hash_}.sigdata"
    try:
        return p if p.is_file() else None
    except OSError:
        return None


def stamps_entry(roots: Iterable[Path], recipe: str, task: str, hash_: str) -> Path | None:
    """By-hash counterpart of :func:`find_task_sigdata`: first root holding the file, no time window."""
    if not (valid_recipe(recipe) and valid_task(task) and valid_hash(hash_)):
        return None
    for root in roots:
        for path in sorted(_sigdata_glob(_Path(root), recipe, task, hash_)):
            try:
                if path.is_file():
                    return path
            except OSError:
                continue
    return None


def earlier_ledger_signatures(
    sstate_dir: Path, recipe: str, task: str, *, before: float
) -> list[tuple[str, Path, float]]:
    """Ledger signatures for ``recipe``/``task`` first captured before ``before``, newest first.

    "Newest" is the last sighting before ``before``: the entry's first-capture mtime, or
    the latest time in its ``.seen`` marker that is also before ``before``, so a revert
    does not leave the replacement looking more recent.
    """
    if not (valid_recipe(recipe) and valid_task(task)):
        return []
    out: list[tuple[str, Path, float]] = []
    markers: dict[str, str] = {}
    d = ledger_root(sstate_dir) / recipe
    prefix, suffix = f"{task}.", ".sigdata"
    try:
        for entry in os.scandir(d):
            name = entry.name
            if not name.startswith(prefix):
                continue
            if name.endswith(suffix + _SEEN_SUFFIX):
                markers[name[: -len(_SEEN_SUFFIX)]] = entry.path
                continue
            if not name.endswith(suffix):
                continue
            try:
                mtime = entry.stat().st_mtime
            except OSError:
                continue
            h = name[len(prefix) : -len(suffix)]
            if valid_hash(h) and mtime < before:
                out.append((h, _Path(entry.path), mtime))
    except OSError:
        return []
    ranked: list[tuple[str, Path, float]] = []
    for h, p, m in out:
        marker = markers.get(p.name)
        sightings = [t for t in _read_sightings(marker) if t < before] if marker else []
        ranked.append((h, p, max([m, *sightings])))
    ranked.sort(key=lambda t: t[2], reverse=True)
    return ranked


class SiginfoScan(NamedTuple):
    """Result of :func:`scan_sstate_siginfo`; unpacks as ``(found, complete)``."""

    found: dict[str, list[tuple[str, Path, float]]]
    complete: bool


def _subdirs(path: str) -> list[str]:
    out: list[str] = []
    try:
        with os.scandir(path) as it:
            for e in it:
                try:
                    if e.is_dir():
                        out.append(e.path)
                except OSError:
                    continue
    except OSError:
        pass
    return out


def scan_sstate_siginfo(
    namespaces: Iterable[Path], wanted: Mapping[str, str], *, before: float, deadline: float
) -> SiginfoScan:
    """One batched pass over ``<ns>/<xx>/<yy>/`` collecting older ``.siginfo`` files.

    Only entry names matching ``sstate:<recipe>:*:<hash>_<task minus do_>.tar.*.siginfo``
    for a wanted recipe/task are stat'ed; mtimes must be strictly below ``before``.
    ``deadline`` is an absolute ``time.monotonic()`` value; the scan stops there and
    reports ``complete=False``.  Each recipe's list is newest first.
    """
    found: dict[str, list[tuple[str, Path, float]]] = {}
    want = {r: t[3:] for r, t in wanted.items() if valid_recipe(r) and valid_task(t)}
    complete = True
    if not want:
        return SiginfoScan(found=found, complete=True)
    for ns in namespaces:
        for xx in _subdirs(str(ns)):
            for yy in _subdirs(xx):
                if time.monotonic() >= deadline:
                    complete = False
                    break
                try:
                    it = os.scandir(yy)
                except OSError:
                    continue
                with it:
                    for e in it:
                        name = e.name
                        if not name.startswith("sstate:") or not name.endswith(".siginfo"):
                            continue
                        parts = name.split(":")
                        if len(parts) < 3 or parts[1] not in want:
                            continue
                        m = _SIGINFO_TAIL_RE.match(parts[-1])
                        if m is None or m.group(2) != want[parts[1]]:
                            continue
                        try:
                            mtime = e.stat().st_mtime
                        except OSError:
                            continue
                        if mtime < before:
                            found.setdefault(parts[1], []).append((m.group(1), _Path(e.path), mtime))
            if not complete:
                break
        if not complete:
            break
    for lst in found.values():
        lst.sort(key=lambda t: t[2], reverse=True)
    return SiginfoScan(found, complete)
