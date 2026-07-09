"""PSI (Pressure Stall Information) time-share report.

bitbake's live build emits ``bb.event.PSIEvent`` samples during the run;
``eventlog.normalize()`` captures them verbatim into ``psi.samples`` (see
that module's ``_PSI_EVENT`` handling) as
``{"time": ..., "cpu": ..., "io": ..., "memory": ...}`` dicts. Per the Linux
kernel PSI documentation, each of ``cpu``/``io``/``memory`` is already an
``avg10`` value - the percentage of the preceding 10 seconds that tasks
stalled on that resource - so no second parser is needed to turn a sample
into a percentage; :func:`pressure_report` only averages values bitbake (via
:func:`bakar.psi.read_psi_avg10`, the same field this module's docstring
points at) already parsed.

This module reuses :data:`bakar.psi.PSI_DIMS` for the fixed dimension order
so the report never drifts from the tuple :func:`bakar.steps.kas_build
._autocalibrate_psi` and its ``psi_loop`` sampler already agree on, rather
than re-deriving a dimension list here.

Like :mod:`bakar.insights_sstate`, this is a single-run, no-persistence pure
function: no filesystem or subprocess access happens inside
:func:`pressure_report` itself. When no samples were captured for the run
(bitbake wrote no ``PSIEvent`` rows, or the raw event log predates schema
version 3), the report signals unavailability rather than fabricating a
0%-pressure summary - a build with no data is not proof the build was
pressure-free.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from bakar.psi import PSI_DIMS

NO_DATA_MESSAGE = "PSI data unavailable"

# Below this avg10 percentage on every dimension, a build is not considered
# resource-pressured. avg10 already expresses "% of the last 10s stalled on
# this resource" (see module docstring), so 10% mirrors the same order of
# magnitude kernel docs use as the threshold for "worth investigating" - it
# is a verdict-only cutoff, not a re-derivation of bakar.psi's calibration
# thresholds (PSI_CLAMP/PSI_MEMORY_FLOOR), which serve a different purpose
# (bounding written config values, not judging a finished run).
LOW_PRESSURE_THRESHOLD = 10.0

_DIM_LABELS = {"cpu": "CPU", "io": "I/O", "memory": "memory"}


@dataclass(frozen=True)
class PressureReport:
    """The PSI time-share report for one run.

    ``available`` is ``False`` when the run captured no PSI samples at all;
    in that case ``time_share`` is empty and ``verdict`` explains why
    (:data:`NO_DATA_MESSAGE`) rather than reporting misleading 0% figures.
    When ``available`` is ``True``, ``time_share`` maps each of
    :data:`bakar.psi.PSI_DIMS` to its mean avg10 percentage across all
    samples, and ``verdict`` is a one-line plain-language summary naming the
    dominant pressure type, or stating the build was not resource-pressured
    when every dimension stays below :data:`LOW_PRESSURE_THRESHOLD`.
    """

    available: bool = False
    time_share: dict[str, float] = field(default_factory=dict)
    verdict: str = NO_DATA_MESSAGE


def pressure_report(psi_samples: list[dict[str, object]]) -> PressureReport:
    """Summarize ``psi_samples`` into per-dimension time-share and a verdict.

    ``psi_samples`` is the ``psi.samples`` list from a normalized
    ``bitbake-events.json`` artifact (see :mod:`bakar.eventlog`): one dict
    per captured ``PSIEvent`` with ``cpu``/``io``/``memory`` avg10 values.
    Samples missing a dimension's value (``None``) are skipped for that
    dimension only, so a partially-populated event does not corrupt the
    other dimensions' averages.

    Returns a :class:`PressureReport` with ``available=False`` when
    ``psi_samples`` is empty or every dimension has zero usable values -
    that state must never be mistaken for a 0%-pressure build.
    """
    sums = dict.fromkeys(PSI_DIMS, 0.0)
    counts = dict.fromkeys(PSI_DIMS, 0)
    for sample in psi_samples:
        for dim in PSI_DIMS:
            value = sample.get(dim)
            if isinstance(value, (int, float)):
                sums[dim] += float(value)
                counts[dim] += 1

    if not any(counts[dim] for dim in PSI_DIMS):
        return PressureReport()

    time_share = {dim: (sums[dim] / counts[dim] if counts[dim] else 0.0) for dim in PSI_DIMS}
    dominant = max(PSI_DIMS, key=lambda dim: time_share[dim])

    if time_share[dominant] < LOW_PRESSURE_THRESHOLD:
        verdict = "build was not resource-pressured (CPU/I/O/memory all below the low-pressure threshold)"
    else:
        pct = time_share[dominant]
        verdict = f"{_DIM_LABELS[dominant]} pressure dominated this build ({pct:.1f}% avg10 time-share)"

    return PressureReport(available=True, time_share=time_share, verdict=verdict)
