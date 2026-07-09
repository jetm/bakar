"""Per-run disk growth report.

A clean Yocto build can fill a disk without warning - the tmp/sstate/deploy
directories grow monotonically over hours and nothing in the build console
tells a developer how much headroom they burned or whether they're about to
run out. This module answers "how much did disk usage grow this run, and did
it blow past a threshold?" from two independent inputs:

- ``disk_samples``: bakar's own periodic host-side disk-usage samples for the
  run, persisted by :class:`bakar.observability.RunLogger` (see
  ``disk_samples_path``, a sibling-file pattern mirroring
  ``sccache_stats_path``/``ccache_stats_path``). Each sample is a
  ``{"time": <epoch seconds>, "used_bytes": <int>}`` dict. This module makes
  no assumption about sampling cadence or how many samples exist - it only
  needs the earliest and latest by ``time``.
- ``events``: the normalized ``bitbake-events.json`` artifact (see
  :mod:`bakar.eventlog`), whose ``disk`` section (schema version 3+) carries
  bitbake-emitted ``MonitorDiskEvent``/``DiskUsageSample`` records in
  ``disk["samples"]`` and ``DiskFull`` records in ``disk["full_events"]``.
  Only ``full_events`` is consulted here - a ``DiskFull`` event is a discrete,
  significant fact surfaced on its own line, never folded into the numeric
  growth figure.

Like :mod:`bakar.insights_sstate`, this is a pure function: no filesystem or
subprocess access happens inside :func:`disk_report` itself. When no
persisted disk samples exist for the run, the function signals unavailability
via :data:`NO_DATA_MESSAGE` rather than fabricating a growth figure (e.g.
reporting 0 bytes of growth, which would look identical to "no growth
happened").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

NO_DATA_MESSAGE = "disk data unavailable for run"


@dataclass(frozen=True)
class DiskReport:
    """The disk-growth report for one run.

    ``growth_bytes`` is ``ending used_bytes - starting used_bytes`` across
    the persisted ``disk_samples``, or ``None`` when no usable samples exist
    (see ``message``). ``full_events`` carries any captured ``DiskFull``
    events verbatim, independent of whether growth could be computed.
    ``warning`` is set only when ``threshold_bytes`` was given and growth
    exceeded it.
    """

    growth_bytes: int | None = None
    full_events: list[dict[str, Any]] = field(default_factory=list)
    warning: str | None = None
    message: str | None = None


def disk_report(
    disk_samples: list[dict[str, Any]] | None,
    events: dict[str, Any],
    threshold_bytes: int | None = None,
) -> DiskReport:
    """Compute the disk-growth report for one run.

    ``disk_samples`` is bakar's own persisted per-run disk-usage samples (see
    module docstring for the expected ``{"time", "used_bytes"}`` shape) -
    NOT the ``events["disk"]["samples"]`` list, which holds bitbake's own
    ``MonitorDiskEvent``/``DiskUsageSample`` records and is not consulted for
    the growth figure. ``events`` is the normalized artifact dict (or
    anything exposing a ``disk`` key with that shape); its ``full_events``
    are cross-referenced and surfaced separately from the numeric growth.

    When ``disk_samples`` is empty, ``None``, or has no rows with a usable
    ``used_bytes`` value, the returned report has ``growth_bytes is None``
    and ``message`` set to :data:`NO_DATA_MESSAGE` - callers must not treat a
    missing report as zero growth. When ``threshold_bytes`` is given and
    growth exceeds it (strictly greater than, not at-or-under), ``warning``
    names both the threshold and the actual growth.
    """
    full_events: list[dict[str, Any]] = []
    disk_section = events.get("disk") if isinstance(events, dict) else None
    if isinstance(disk_section, dict):
        raw_full = disk_section.get("full_events")
        if isinstance(raw_full, list):
            full_events = [row for row in raw_full if isinstance(row, dict)]

    valid_samples = [
        row
        for row in (disk_samples or [])
        if isinstance(row, dict) and isinstance(row.get("used_bytes"), (int, float))
    ]
    # A single sample has no earlier reading to diff against - growth is
    # indeterminate, not zero. Reporting `0` here would be indistinguishable
    # from "measured and confirmed no growth happened" (see module docstring).
    if len(valid_samples) < 2:
        return DiskReport(full_events=full_events, message=NO_DATA_MESSAGE)

    ordered = sorted(valid_samples, key=lambda row: row.get("time") or 0)
    growth_bytes = int(ordered[-1]["used_bytes"] - ordered[0]["used_bytes"])

    warning = None
    if threshold_bytes is not None and growth_bytes > threshold_bytes:
        warning = (
            f"disk growth {growth_bytes} bytes exceeds threshold {threshold_bytes} bytes"
        )

    return DiskReport(growth_bytes=growth_bytes, full_events=full_events, warning=warning)
