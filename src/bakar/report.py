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
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from bakar.layers import collect_layer_hashes
from bakar.triage import _last_event_matching

if TYPE_CHECKING:
    from bakar.config import BuildConfig
    from bakar.layers import LayerHash

_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"


@dataclass(frozen=True)
class ReportSummary:
    run_id: str
    status: str  # "success" or "failure"
    duration_s: float | None = None
    deploy_dir: str | None = None
    image_size: int | None = None
    peak_tmp_bytes: int | None = None
    layers: list[LayerHash] = field(default_factory=list)
    build_revision: str | None = None


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


def _peak_tmp_bytes(du_path: Path) -> int | None:
    """Return the max of the second TAB-column across ``du.tsv`` rows.

    Each row is ``<epoch>\\t<bytes>``. Rows without a parseable second column
    are skipped. Returns ``None`` when the file is absent or holds no usable
    samples.
    """
    if not du_path.is_file():
        return None
    peak: int | None = None
    try:
        text = du_path.read_text()
    except OSError:
        return None
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        try:
            value = int(parts[1].strip())
        except ValueError:
            continue
        if peak is None or value > peak:
            peak = value
    return peak


def assemble_report(run_dir: Path, cfg: BuildConfig) -> ReportSummary:
    """Assemble a best-effort summary of the run in ``run_dir``.

    Reads ``events.jsonl`` for the ``run_start``/``run_end`` timestamps and the
    ``kas_build`` ``step_ok``/``step_fail`` outcome, ``du.tsv`` for the peak
    build-tmp size, and ``collect_layer_hashes`` for the per-layer SHAs. Status
    is ``"success"`` when a ``kas_build`` ``step_ok`` exists, else ``"failure"``.
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

    return ReportSummary(
        run_id=run_dir.name,
        status=status,
        duration_s=_duration_s(run_start, run_end),
        deploy_dir=deploy_dir,
        image_size=_largest_image_size(deploy_dir),
        peak_tmp_bytes=_peak_tmp_bytes(run_dir / "du.tsv"),
        layers=layers,
        build_revision=build_revision,
    )
