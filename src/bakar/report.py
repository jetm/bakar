"""Success-path summary for a completed build run.

Reads a run directory produced by :mod:`bakar.observability` and assembles a
:class:`ReportSummary` - the complement to :mod:`bakar.triage`, which only
handles failures. Every field is best-effort: a missing file, an absent JSON
field, or an unparseable timestamp yields ``None`` rather than an exception, so
``assemble_report`` is safe to call against partial or legacy run directories.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from bakar.layers import collect_layer_hashes
from bakar.task_rollup import FamilyStat, compute_task_rollup
from bakar.triage import _last_event_matching

if TYPE_CHECKING:
    from bakar.config import BuildConfig
    from bakar.layers import LayerHash

_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"


@dataclass(frozen=True)
class LangCacheStat:
    """sccache hit/miss counts and derived hit-rate for one language."""

    hits: int = 0
    misses: int = 0
    hit_rate: float = 0.0


@dataclass(frozen=True)
class ReportSummary:
    run_id: str
    status: str  # "success" or "failure"
    duration_s: float | None = None
    deploy_dir: str | None = None
    image_size: int | None = None
    layers: list[LayerHash] = field(default_factory=list)
    build_revision: str | None = None
    sstate_wanted: int | None = None
    sstate_local: int | None = None
    sstate_mirrors: int | None = None
    sstate_missed: int | None = None
    sstate_current: int | None = None
    sstate_match_pct: int | None = None
    sstate_complete_pct: int | None = None
    buildhistory_imagesize_kib: int | None = None
    top_packages: list[tuple[str, int]] = field(default_factory=list)
    pkg_count: int | None = None
    layers_dirty: list[str] = field(default_factory=list)
    has_buildhistory: bool = False
    cache_by_language: dict[str, LangCacheStat] = field(default_factory=dict)
    dist_by_node: dict[str, int] = field(default_factory=dict)
    task_family_rollup: dict[str, FamilyStat] = field(default_factory=dict)
    go_compile_seconds: float = 0.0


def _parse_ts(rec: dict | None) -> datetime | None:
    if rec is None:
        return None
    raw = rec.get("ts")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.strptime(raw, _TS_FMT)
    except ValueError:
        return None


def _duration_s(run_start: dict | None, run_end: dict | None) -> float | None:
    start = _parse_ts(run_start)
    end = _parse_ts(run_end)
    if start is None or end is None:
        return None
    return (end - start).total_seconds()


def _largest_image_size(deploy_dir: str | None) -> int | None:
    """Return the byte size of the largest regular file under ``deploy_dir``.

    The deploy directory holds the rootfs/wic image alongside smaller boot
    artifacts; the largest file is the deployed image in practice. Returns
    ``None`` when the directory is absent, empty, or unreadable.
    """
    if not deploy_dir:
        return None
    root = Path(deploy_dir)
    if not root.is_dir():
        return None
    largest: int | None = None
    try:
        for entry in root.rglob("*"):
            try:
                if not entry.is_file():
                    continue
                size = entry.stat().st_size
            except OSError:
                continue
            if largest is None or size > largest:
                largest = size
    except OSError:
        return None
    return largest


# Named sub-patterns scanned individually against the Sstate summary line so a
# field reorder across Yocto releases still matches each value by its label.
_SSTATE_FIELDS: dict[str, re.Pattern[str]] = {
    "sstate_wanted": re.compile(r"Wanted (\d+)"),
    "sstate_local": re.compile(r"Local (\d+)"),
    "sstate_mirrors": re.compile(r"Mirrors (\d+)"),
    "sstate_missed": re.compile(r"Missed (\d+)"),
    "sstate_current": re.compile(r"Current (\d+)"),
    "sstate_match_pct": re.compile(r"(\d+)% match"),
    "sstate_complete_pct": re.compile(r"(\d+)% complete"),
}


def _parse_sstate_summary(kas_log: Path) -> dict[str, int | None]:
    """Parse the ``Sstate summary:`` line from ``kas_log`` field-by-field.

    Returns a mapping of the seven sstate field names to their integer values.
    Each field is matched by its own named sub-pattern (``Wanted N``, ``Local
    N``, ...) rather than positionally, so a field reorder across Yocto releases
    still resolves each value. A missing file, a missing summary line, or a
    field absent from the line leaves that field ``None`` without raising.
    """
    none_result: dict[str, int | None] = dict.fromkeys(_SSTATE_FIELDS)
    if not kas_log.is_file():
        return none_result
    try:
        text = kas_log.read_text()
    except OSError:
        return none_result

    summary_line = next((ln for ln in reversed(text.splitlines()) if "Sstate summary:" in ln), None)
    if summary_line is None:
        return none_result

    result: dict[str, int | None] = {}
    for name, pattern in _SSTATE_FIELDS.items():
        match = pattern.search(summary_line)
        result[name] = int(match.group(1)) if match else None
    return result


def _read_imagesize_kib(image_info: Path) -> int | None:
    """Return the ``IMAGESIZE`` KiB value from a buildhistory ``image-info.txt``.

    The file holds ``KEY = VALUE`` lines; ``IMAGESIZE`` is the rootfs size in
    KiB. Returns ``None`` when the file is absent, unreadable, or the value is
    missing or non-integer.
    """
    try:
        text = image_info.read_text()
    except OSError:
        return None
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() == "IMAGESIZE":
            try:
                return int(value.strip())
            except ValueError:
                return None
    return None


def _read_top_packages(pkg_sizes: Path, limit: int = 10) -> list[tuple[str, int]]:
    """Return the top ``limit`` ``(package, size_kib)`` rows by size.

    ``installed-package-sizes.txt`` holds ``<size> KiB <pkg>`` rows already
    sorted descending (tab-separated as written by buildhistory). Columns are
    split on any whitespace so space-padded variants still parse. Malformed rows
    (wrong column count or non-integer size) are skipped without aborting.
    Returns ``[]`` when the file is absent or unreadable.
    """
    try:
        text = pkg_sizes.read_text()
    except OSError:
        return []
    rows: list[tuple[str, int]] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        try:
            size = int(parts[0])
        except ValueError:
            continue
        pkg = parts[2]
        if not pkg:
            continue
        rows.append((pkg, size))
        if len(rows) >= limit:
            break
    return rows


def _read_pkg_count(pkg_names: Path) -> int | None:
    """Return the number of non-empty lines in ``installed-package-names.txt``.

    Each line names one installed package. Returns ``None`` when the file is
    absent or unreadable.
    """
    try:
        text = pkg_names.read_text()
    except OSError:
        return None
    return sum(1 for line in text.splitlines() if line.strip())


def _read_dirty_layers(metadata_revs: Path) -> list[str]:
    """Return the layer names flagged ``-- modified`` in ``metadata-revs``.

    Each row is ``<layer> = <branch>:<sha>`` with a trailing ``-- modified``
    marker when the layer's tree is uncommitted (oe.buildcfg.get_layer_revisions
    output). The leading token is already the layer name; ``Path(...).name``
    leaves it unchanged and also tolerates a path-shaped token. Returns ``[]``
    when the file is absent or unreadable.
    """
    try:
        text = metadata_revs.read_text()
    except OSError:
        return []
    dirty: list[str] = []
    for line in text.splitlines():
        if not line.rstrip().endswith("-- modified"):
            continue
        tokens = line.split()
        if tokens:
            dirty.append(Path(tokens[0]).name)
    return dirty


def _parse_buildhistory(cfg: BuildConfig) -> dict | None:
    """Parse static buildhistory artifacts under ``cfg.bsp_root``.

    Detects ``<bsp_root>/build/buildhistory``, gated on the ``images/``
    subdirectory or a ``metadata-revs`` file being present. Parses
    ``image-info.txt``, ``installed-package-sizes.txt``, and
    ``installed-package-names.txt`` from the per-image directory (globbed under
    ``images/*/*/*/``), plus the top-level ``metadata-revs``. Returns ``None``
    when the buildhistory directory is absent or fails the presence gate, so the
    caller can skip the section entirely. Each parsed field degrades to
    ``None``/``[]`` on a missing or malformed file without raising.
    """
    root = cfg.bsp_root / "build" / "buildhistory"
    if not root.is_dir():
        return None
    images_dir = root / "images"
    metadata_revs = root / "metadata-revs"
    if not images_dir.is_dir() and not metadata_revs.is_file():
        return None

    imagesize_kib: int | None = None
    top_packages: list[tuple[str, int]] = []
    pkg_count: int | None = None
    for image_info in images_dir.glob("*/*/*/image-info.txt"):
        imagesize_kib = _read_imagesize_kib(image_info)
        top_packages = _read_top_packages(image_info.parent / "installed-package-sizes.txt")
        pkg_count = _read_pkg_count(image_info.parent / "installed-package-names.txt")
        break

    return {
        "buildhistory_imagesize_kib": imagesize_kib,
        "top_packages": top_packages,
        "pkg_count": pkg_count,
        "layers_dirty": _read_dirty_layers(metadata_revs),
    }


def _read_sccache_stats(stats_path: Path) -> dict:
    """Return the persisted ``sccache-stats.json`` doc, or ``{}`` when absent.

    The file is the serialized ``daemon_doc`` dict written at build end. It is
    absent for container builds and runs predating this feature; a missing file
    or a malformed/non-object payload yields ``{}`` without raising.
    """
    try:
        with stats_path.open("r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except OSError, ValueError:
        return {}
    return doc if isinstance(doc, dict) else {}


def _cache_by_language(stats: dict) -> dict[str, LangCacheStat]:
    """Build the per-language hit/miss/hit-rate map from a stats doc.

    Reads the ``hits_by_lang``/``misses_by_lang`` dicts (keyed by sccache's
    language display name) and derives ``hit_rate = 100 * hits / (hits + misses)``
    per language, guarding a zero total. Languages present in either dict are
    covered; a missing or non-dict source yields ``{}``.
    """
    hits = stats.get("hits_by_lang")
    misses = stats.get("misses_by_lang")
    hits = hits if isinstance(hits, dict) else {}
    misses = misses if isinstance(misses, dict) else {}
    langs = [*hits, *(lang for lang in misses if lang not in hits)]
    result: dict[str, LangCacheStat] = {}
    for lang in langs:
        h = hits.get(lang, 0)
        m = misses.get(lang, 0)
        total = h + m
        rate = 100.0 * h / total if total else 0.0
        result[lang] = LangCacheStat(hits=h, misses=m, hit_rate=rate)
    return result


def _dist_by_node(stats: dict) -> dict[str, int]:
    """Return the per-node distribution counts from a stats doc.

    Reads the ``per_node`` dict (node address -> compile count). A missing or
    non-dict source, or a non-integer count, yields an empty/filtered map.
    """
    per_node = stats.get("per_node")
    if not isinstance(per_node, dict):
        return {}
    return {str(addr): count for addr, count in per_node.items() if isinstance(count, int)}


def assemble_report(run_dir: Path, cfg: BuildConfig) -> ReportSummary:
    """Assemble a best-effort summary of the run in ``run_dir``.

    Reads ``events.jsonl`` for the ``run_start``/``run_end`` timestamps and the
    ``kas_build`` ``step_ok``/``step_fail`` outcome, and ``collect_layer_hashes``
    for the per-layer SHAs. Status is ``"success"`` when a ``kas_build``
    ``step_ok`` exists, else ``"failure"``.
    """
    events_path = run_dir / "events.jsonl"

    run_start = _last_event_matching(events_path, "run_start")
    run_end = _last_event_matching(events_path, "run_end")

    step_ok = None
    if events_path.is_file():
        for line in events_path.read_text().splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("event") == "step_ok" and rec.get("step") == "kas_build":
                step_ok = rec

    status = "success" if step_ok is not None else "failure"
    deploy_dir = step_ok.get("deploy_dir") if step_ok else None

    layers = collect_layer_hashes(cfg)
    build_revision: str | None = (
        hashlib.sha1("".join(sorted(layer.short_hash for layer in layers)).encode()).hexdigest()[:12]
        if layers
        else None
    )

    sstate = _parse_sstate_summary(run_dir / "kas.log")
    # Parse buildhistory once here; the command derives its display gate from
    # ``has_buildhistory`` rather than re-parsing the tree. The dict keys match
    # the ReportSummary field names exactly, so both maps splat directly.
    buildhistory = _parse_buildhistory(cfg)

    stats = _read_sccache_stats(run_dir / "sccache-stats.json")
    rollup = compute_task_rollup(run_dir / "bitbake-events.json")

    return ReportSummary(
        run_id=run_dir.name,
        status=status,
        duration_s=_duration_s(run_start, run_end),
        deploy_dir=deploy_dir,
        image_size=_largest_image_size(deploy_dir),
        layers=layers,
        build_revision=build_revision,
        has_buildhistory=buildhistory is not None,
        cache_by_language=_cache_by_language(stats),
        dist_by_node=_dist_by_node(stats),
        task_family_rollup=rollup.families,
        go_compile_seconds=rollup.go_compile_seconds,
        **sstate,
        **(buildhistory or {}),
    )
