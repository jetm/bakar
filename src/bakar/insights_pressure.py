"""PSI (Pressure Stall Information) time-share report.

In production, ``psi_samples`` comes from bakar's own host-side sampler
(``steps/kas_build.py``'s ``psi_loop``, persisted via
``RunLogger.persist_psi_samples`` to the ``psi-samples.json`` sibling file) -
NOT from the normalized ``bitbake-events.json`` artifact's ``psi.samples``.
``eventlog.normalize()`` also captures raw ``bb.event.PSIEvent`` records into
``psi.samples``, but that event class isn't defined anywhere in the vendored
bitbake tree, so its real attribute shape can't be verified from this repo;
do not feed it to :func:`pressure_report` without first confirming it carries
flat ``avg10`` floats per dimension, not nested sub-metric dicts. bakar's own
sampler values are unambiguous: each of ``cpu``/``io``/``memory`` is a flat
``avg10`` percentage read via :func:`bakar.psi.read_psi_avg10` (the same
field the live ``_autocalibrate_psi`` throttling logic already parses), so no
second parser is needed to turn a sample into a percentage.

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
    When ``available`` is ``True``, ``time_share`` maps each dimension in
    :data:`bakar.psi.PSI_DIMS` that had at least one usable sample to its mean
    avg10 percentage - a dimension with zero usable values is omitted
    entirely rather than reported as ``0.0`` (which would be indistinguishable
    from a measured, confirmed 0% pressure reading). ``verdict`` is a one-line
    plain-language summary naming the dominant pressure type among the
    present dimensions, or stating the build was not resource-pressured when
    every present dimension stays below :data:`LOW_PRESSURE_THRESHOLD`.
    """

    available: bool = False
    time_share: dict[str, float] = field(default_factory=dict)
    verdict: str = NO_DATA_MESSAGE


def pressure_report(psi_samples: list[dict[str, object]]) -> PressureReport:
    """Summarize ``psi_samples`` into per-dimension time-share and a verdict.

    ``psi_samples`` is bakar's own persisted host-side sample list (see
    module docstring): one dict per sample with flat ``cpu``/``io``/``memory``
    avg10 values. Samples missing a dimension's value, or carrying a
    non-numeric value for it (``None`` is skipped silently; any other
    non-numeric value, e.g. a nested dict, is also skipped rather than
    raising), are excluded for that dimension only, so a partially-populated
    or malformed sample does not corrupt the other dimensions' averages.

    Returns a :class:`PressureReport` with ``available=False`` when
    ``psi_samples`` is empty or every dimension has zero usable values -
    that state must never be mistaken for a 0%-pressure build. A dimension
    with zero usable values is omitted from ``time_share`` entirely (rather
    than defaulting to ``0.0``) so a single stalled dimension (e.g.
    ``read_psi_avg10`` failing for ``memory`` on a host without that
    controller) never gets misread as "measured, confirmed 0% pressure".
    """
    sums = dict.fromkeys(PSI_DIMS, 0.0)
    counts = dict.fromkeys(PSI_DIMS, 0)
    for sample in psi_samples:
        if not isinstance(sample, dict):
            continue
        for dim in PSI_DIMS:
            value = sample.get(dim)
            if isinstance(value, (int, float)):
                sums[dim] += float(value)
                counts[dim] += 1

    if not any(counts[dim] for dim in PSI_DIMS):
        return PressureReport()

    time_share = {dim: sums[dim] / counts[dim] for dim in PSI_DIMS if counts[dim]}
    dominant = max(time_share, key=lambda dim: time_share[dim])

    if time_share[dominant] < LOW_PRESSURE_THRESHOLD:
        verdict = "build was not resource-pressured (CPU/I/O/memory all below the low-pressure threshold)"
    else:
        pct = time_share[dominant]
        verdict = f"{_DIM_LABELS[dominant]} pressure dominated this build ({pct:.1f}% avg10 time-share)"

    return PressureReport(available=True, time_share=time_share, verdict=verdict)
