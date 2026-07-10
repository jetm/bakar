"""Aggregate the mold/bfd link-timing log into a comparable report.

The ``ld-timing-wrapper.sh`` interposer (installed by ``mold.bbclass`` as both
``ld.mold`` and ``ld.bfd`` in the ``-B`` wrapper dir) appends one JSON object
per link to the build-global log named by ``BAKAR_MOLD_LINKLOG``. Each record
is one line with the shared schema (tasks 1.3 / 4.2 / 7.1)::

    {"linker": str, "recipe": str, "output": str,
     "wall_ms": int, "nproc": int|null, "loadavg": float|null,
     "threads": int|null}

This module parses that log and reports the summed link work: Σ(wall_ms), the
invocation count, a per-linker breakdown, and the recorded covariates
(``nproc``/``loadavg``/``threads``) retained per record so contended-parallel
numbers can be interpreted. It never imports ``bb`` - it reads only the log the
wrapper already wrote.

**Σ validity (read before trusting the total).** Σ(wall_ms) is the sum of
per-link durations, NOT build wall-clock. It is valid only from the FIRST cold
instrumented run of a recipe set: a re-run restores ``do_compile`` from sstate,
so most links are never re-executed and the wrapper is never invoked for them -
Σ then under-counts and is not comparable to the cold total. The headline
mold-versus-bfd number therefore comes from a forced, dirtied relink
(``bitbake -f -c compile <target>`` after dirtying the linked artifact) on an
idle machine, which :func:`compare_relink` compares arm-for-arm.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable


@dataclass(frozen=True)
class LinkRecord:
    """One wrapper-logged link, with its covariates retained.

    ``nproc``/``loadavg``/``threads`` are optional: the wrapper writes JSON
    ``null`` when it could not determine them, which parses to ``None`` here.
    They are never dropped so contended-parallel Σ values can be normalised.
    """

    linker: str
    recipe: str
    output: str
    wall_ms: int
    nproc: int | None = None
    loadavg: float | None = None
    threads: int | None = None


@dataclass(frozen=True)
class LinkerStat:
    """Summed link duration (ms) and invocation count for one linker."""

    total_wall_ms: int = 0
    count: int = 0


@dataclass(frozen=True)
class LinkStatsReport:
    """Aggregate of one link-timing log.

    ``total_wall_ms`` is Σ(wall_ms) across every valid record and
    ``count`` is the invocation count. ``per_linker`` breaks both down by
    ``linker`` name (e.g. ``ld.mold`` vs ``ld.bfd``). ``records`` retains every
    parsed record so the ``nproc``/``loadavg``/``threads`` covariates survive
    into the report rather than being collapsed away.
    """

    total_wall_ms: int = 0
    count: int = 0
    per_linker: dict[str, LinkerStat] = field(default_factory=dict)
    records: tuple[LinkRecord, ...] = ()


@dataclass(frozen=True)
class RelinkComparison:
    """Headline mold-versus-baseline comparison from two forced relinks.

    ``speedup`` is baseline Σ divided by mold Σ (``>1`` means mold is faster);
    it is ``None`` when the mold arm summed to zero, which also flags a relink
    that executed no real link (a no-op headline).
    """

    mold: LinkStatsReport
    baseline: LinkStatsReport
    delta_ms: int
    speedup: float | None


def _opt_int(value: object) -> int | None:
    """Coerce a covariate to ``int`` or ``None`` (``null``/missing/non-numeric)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except TypeError, ValueError:
        return None


def _opt_float(value: object) -> float | None:
    """Coerce a covariate to ``float`` or ``None`` (``null``/missing/non-numeric)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except TypeError, ValueError:
        return None


def _record_from(obj: object) -> LinkRecord | None:
    """Build a :class:`LinkRecord` from one parsed JSON object, or ``None``.

    A record is usable only when it is a dict carrying the string ``linker``
    and a numeric ``wall_ms``; anything else (a short line, a partial write, a
    non-object) returns ``None`` so the caller can skip it without raising.
    ``recipe``/``output`` default to empty strings when absent; the covariates
    default to ``None``.
    """
    if not isinstance(obj, dict):
        return None
    linker = obj.get("linker")
    if not isinstance(linker, str):
        return None
    wall = obj.get("wall_ms")
    if isinstance(wall, bool) or not isinstance(wall, (int, float)):
        return None

    recipe = obj.get("recipe")
    output = obj.get("output")
    return LinkRecord(
        linker=linker,
        recipe=recipe if isinstance(recipe, str) else "",
        output=output if isinstance(output, str) else "",
        wall_ms=int(wall),
        nproc=_opt_int(obj.get("nproc")),
        loadavg=_opt_float(obj.get("loadavg")),
        threads=_opt_int(obj.get("threads")),
    )


def _lines_from(source: Path | str | Iterable[str]) -> list[str]:
    """Return the raw log lines from a path or an already-split iterable.

    A :class:`~pathlib.Path` or ``str`` is read as the ``BAKAR_MOLD_LINKLOG``
    file; a missing or unreadable file yields no lines. Any other iterable is
    treated as the log lines verbatim (for tests and in-memory callers).
    """
    if isinstance(source, (str, Path)):
        try:
            return Path(source).read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
    return list(source)


def parse_linklog(source: Path | str | Iterable[str]) -> tuple[LinkRecord, ...]:
    """Parse the JSON-lines link log into records, skipping bad lines.

    ``source`` is the log path or an iterable of its lines. Blank lines, lines
    that are not valid JSON, and objects missing the required ``linker``/
    ``wall_ms`` fields (e.g. a truncated final line from a crashed link) are
    dropped silently, so a partially-written log still aggregates cleanly.

    Returns a tuple so :func:`aggregate_linklog` can hand it straight to
    :attr:`LinkStatsReport.records` (also a tuple) without re-copying.
    """
    records: list[LinkRecord] = []
    for line in _lines_from(source):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except ValueError:
            continue
        record = _record_from(obj)
        if record is not None:
            records.append(record)
    return tuple(records)


def aggregate_linklog(source: Path | str | Iterable[str]) -> LinkStatsReport:
    """Aggregate a link log into a :class:`LinkStatsReport`.

    Reports Σ(wall_ms) as ``total_wall_ms``, the invocation ``count``, a
    ``per_linker`` breakdown, and the full parsed ``records`` (covariates
    retained). See the module docstring: Σ is valid only from the first cold
    instrumented run.
    """
    records = parse_linklog(source)

    total_wall_ms = 0
    per_linker_total: dict[str, int] = {}
    per_linker_count: dict[str, int] = {}
    for record in records:
        total_wall_ms += record.wall_ms
        per_linker_total[record.linker] = per_linker_total.get(record.linker, 0) + record.wall_ms
        per_linker_count[record.linker] = per_linker_count.get(record.linker, 0) + 1

    per_linker = {
        linker: LinkerStat(total_wall_ms=per_linker_total[linker], count=per_linker_count[linker])
        for linker in per_linker_total
    }
    return LinkStatsReport(
        total_wall_ms=total_wall_ms,
        count=len(records),
        per_linker=per_linker,
        records=records,
    )


def compare_relink(
    mold_source: Path | str | Iterable[str],
    baseline_source: Path | str | Iterable[str],
) -> RelinkComparison:
    """Compare a mold-arm log against a baseline (bfd) log for the headline.

    Each argument is one forced-relink arm's link log. Returns the two
    aggregates plus ``delta_ms`` (baseline Σ minus mold Σ) and ``speedup``
    (baseline Σ / mold Σ, ``None`` when the mold arm summed to zero). Per the
    module docstring, feed this only forced, dirtied relinks - not sstate-warm
    re-runs.
    """
    mold = aggregate_linklog(mold_source)
    baseline = aggregate_linklog(baseline_source)
    delta_ms = baseline.total_wall_ms - mold.total_wall_ms
    speedup = baseline.total_wall_ms / mold.total_wall_ms if mold.total_wall_ms > 0 else None
    return RelinkComparison(
        mold=mold,
        baseline=baseline,
        delta_ms=delta_ms,
        speedup=speedup,
    )
