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
    sstate_wanted: int | None = None
    sstate_local: int | None = None
    sstate_mirrors: int | None = None
    sstate_missed: int | None = None
    sstate_current: int | None = None
    sstate_match_pct: int | None = None
    sstate_complete_pct: int | None = None


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

    summary_line: str | None = None
    for line in text.splitlines():
        if "Sstate summary:" in line:
            summary_line = line
    if summary_line is None:
        return none_result

    result: dict[str, int | None] = {}
    for name, pattern in _SSTATE_FIELDS.items():
        match = pattern.search(summary_line)
        result[name] = int(match.group(1)) if match else None
    return result


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

    sstate = _parse_sstate_summary(run_dir / "kas.log")

    return ReportSummary(
        run_id=run_dir.name,
        status=status,
        duration_s=_duration_s(run_start, run_end),
        deploy_dir=deploy_dir,
        image_size=_largest_image_size(deploy_dir),
        peak_tmp_bytes=_peak_tmp_bytes(run_dir / "du.tsv"),
        layers=layers,
        build_revision=build_revision,
        sstate_wanted=sstate["sstate_wanted"],
        sstate_local=sstate["sstate_local"],
        sstate_mirrors=sstate["sstate_mirrors"],
        sstate_missed=sstate["sstate_missed"],
        sstate_current=sstate["sstate_current"],
        sstate_match_pct=sstate["sstate_match_pct"],
        sstate_complete_pct=sstate["sstate_complete_pct"],
    )
