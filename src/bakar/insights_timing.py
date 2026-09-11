"""Per-task timing and top-N-slowest report.

bitbake records per-task ``started``/``completed`` timestamps in the
normalized ``bitbake-events.json`` artifact (see :mod:`bakar.eventlog`).
:func:`timing_report` turns those rows into a ranked "where did my build time
go" view: the top-N slowest individual tasks, each annotated with the prior
cross-build mean/stddev already tracked by :mod:`bakar.task_timings` (no
second baseline store is built here).

The tasks-list extraction and missing/negative-duration guard reuse
:func:`bakar.task_rollup.tasks_from` rather than re-parsing the ``tasks``
list a third time (see design.md's "reuse ``tasks_from``" decision).

This module is the orchestrator: :func:`timing_report` builds the executed-task
identity set and the duration list, then delegates to three sibling modules for
the named clusters that used to live here directly:

- :mod:`bakar.insights_joins` - :class:`~bakar.insights_joins.GraphJoin` and
  :class:`~bakar.insights_joins.BuildstatsJoin`, and the shared join-rate gate
  both are built on.
- :mod:`bakar.insights_critical_path` - :class:`~bakar.insights_critical_path.CriticalPath`,
  the longest dependency-respecting chain through the build, each node
  weighted by the elapsed time of the executed task that resolves to it
  rather than by a recipe's summed task seconds. It is opt-in: callers pass a
  ``dependency_source`` callable that returns the ``(dot_text,
  buildlist_text)`` pair. In production that callable reads the run's own
  already-captured ``task-depends.dot`` (see
  ``commands.insights._dependency_source``) rather than invoking a fresh
  ``bitbake -g <recipe>`` - the graph capture happens once, at build time, and
  this module only ever reads it back. Tests supply a canned fixture. When
  ``dependency_source`` is omitted, or it raises, or the resulting graph is
  empty/cyclic, :class:`~bakar.insights_critical_path.CriticalPath` reports
  ``available=False`` with an explanatory ``note`` - the duration and top-N
  sections below never depend on this section's success.
- :mod:`bakar.insights_churn` - :class:`~bakar.insights_churn.TaskChurn`,
  per-task-type process-churn and I/O aggregation over the joined buildstats
  records.

The names those modules define are re-exported here (``CriticalPath``,
``GraphJoin``, ``BuildstatsJoin``, ``TaskChurn``, join constants, etc.) so
this module's import surface - and every existing caller and test importing
from :mod:`bakar.insights_timing` - keeps working unchanged; this was an
internal reorganisation, not an API change.

What stays here, beyond the orchestrator itself: the CPU floor and the
concurrency floor built on it (both need the artifact's recorded build-host
core count, which is a property of THIS run rather than of either join or the
critical path), the sstate regime detection, and the build/correlation window
helpers the buildstats capture is selected against.
"""

from __future__ import annotations

import functools
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from bakar import task_timings
from bakar.insights_churn import CHURN_COLUMNS, CHURN_WIDTHS, TaskChurn, _compute_churn
from bakar.insights_critical_path import CRITICAL_PATH_TOP_N, CriticalPath, _compute_critical_path
from bakar.insights_joins import (
    _NOT_EXECUTED_OUTCOME,
    JOIN_RATE_THRESHOLD,
    UNJOINED_SAMPLE,
    BuildstatsJoin,
    GraphJoin,
    _compute_graph_join,
    _compute_join,
    _parse_dependency_graph,
    _resolve_graph_node,
)
from bakar.task_rollup import tasks_from

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from bakar.buildstats import BuildstatsRun
    from bakar.insights_joins import _ExecutedTask

#: Re-exported for backward compatibility: this module used to define these
#: names directly before the join/critical-path/churn clusters moved to
#: :mod:`bakar.insights_joins`, :mod:`bakar.insights_critical_path` and
#: :mod:`bakar.insights_churn` respectively. Existing callers and tests that
#: import them from here keep working unchanged.
__all__ = [
    "BASIS_NOTE",
    "BINDING_LABELS",
    "CHURN_COLUMNS",
    "CHURN_WIDTHS",
    "CRITICAL_PATH_TOP_N",
    "DEFAULT_TOP_N",
    "JOIN_RATE_THRESHOLD",
    "UNJOINED_SAMPLE",
    "BuildstatsJoin",
    "ConcurrencyFloor",
    "CpuFloor",
    "CriticalPath",
    "GraphJoin",
    "Regime",
    "TaskChurn",
    "TaskDuration",
    "TimingReport",
    "_compute_concurrency_floor",
    "_resolve_graph_node",
    "build_window",
    "correlation_window",
    "timing_report",
]

DEFAULT_TOP_N = 10


@dataclass(frozen=True)
class TaskDuration:
    """One task's wall-clock duration with optional baseline context.

    ``baseline_mean``/``baseline_stddev`` are ``None`` when no prior baseline
    exists for this task's ``"<recipe-sans-version>:<task>"`` key.
    """

    recipe: str
    task: str
    duration: float
    baseline_mean: float | None = None
    baseline_stddev: float | None = None


@dataclass(frozen=True)
class Regime:
    """Which sstate regime a run was in: how much of it was restored, not built.

    Two builds of the same target in different regimes take wildly different
    times - measured on this fleet, 26.4 min cold against 8.9 min seeded, a 65%
    swing - and every other number in this report is only comparable against
    another run in the SAME regime. A timing report that omits it invites
    exactly the comparison that cannot be made, which is not hypothetical: this
    project's benchmark baseline was invalidated when an sstate seed appeared
    mid-campaign and no run recorded which side of it that run was on.

    ``measured`` is the distinction that matters and it is not the same as
    ``covered == 0``. ``bakar.eventlog`` seeds this block with zeros and returns
    it VERBATIM when bitbake's raw event log is missing - a build killed during
    parsing, or one whose bitbake never emitted the stats - so an all-zero block
    is indistinguishable by inspection from a genuine cold build where nothing
    was restored. The discriminator is that the same early return also yields an
    empty ``tasks`` list, and a build that ran anything has a non-empty one.
    """

    measured: bool = False
    covered: int = 0
    notcovered: int = 0
    total: int = 0
    note: str = "regime unknown"

    @property
    def covered_pct(self) -> float:
        """Share of tasks restored from sstate, or 0.0 when nothing was counted."""
        return (100.0 * self.covered / self.total) if self.total else 0.0


@dataclass(frozen=True)
class CpuFloor:
    """The CPU-only lower bound: joined CPU seconds divided by the build host's cores.

    The divisor comes from the ``host`` block the event-log artifact recorded ON
    THE BUILD HOST (see :func:`bakar.eventlog._host_block`), never from
    ``os.cpu_count()`` here. That divergence from the reference analyser is
    deliberate (design D4): the reference reads ``nproc`` at analysis time and
    its comparability check only fires in one direction, so analysing a capture
    on a machine with MORE cores lowers the floor and overstates the achievable
    saving with nothing printed. Reading a recorded count makes the same capture
    yield the same floor on any machine.

    An artifact predating that block has no recorded count. That degrades with
    an explicit note, following
    :func:`bakar.insights_critical_path._compute_critical_path`'s precedent -
    substituting the analysing host's count would be exactly the defect above,
    arrived at by fallback instead of by design.

    ``seconds`` is ``None`` unless ``available``; in particular a refused join
    hands this section ``cpu_seconds=None`` and there is no other input it could
    reach for, so a floor over a partial join has no expressible form here.
    """

    available: bool = False
    seconds: float | None = None
    divisor: int | None = None
    note: str = "CPU floor unavailable: no buildstats source supplied"

    def report_lines(self) -> list[str]:
        """Render this section as plain text lines.

        The available case NAMES its divisor and where the divisor came from, so
        a capture analysed on a different machine than it was taken on stays
        auditable from the output alone. A bare "floor: 900s" cannot be checked
        by anyone who was not present at the build.
        """
        return [f"  {self.note}"]


#: How each bound is named in the output. A reader has to be able to tell a
#: dependency-bound build from a throughput-bound one at a glance; printing the
#: max alone leaves the two indistinguishable, which is the confusion that let a
#: CPU-only 11.9% headroom read as actionable when the real figure was 1.1%.
BINDING_LABELS = {"cpu": "CPU capacity", "path": "the critical path"}

#: Stated beside every available concurrency floor. As of design D5, both the
#: critical path and the CPU floor are task-level - each critical-path node
#: carries only the elapsed time of the executed task that resolved to it, on
#: the same per-task-seconds basis the CPU floor uses. The difference between
#: the two bounds is therefore a quantity a reader may reason about, not two
#: measurements on different bases.
BASIS_NOTE = (
    "basis: both the critical path and the CPU floor are task-level - the path weights each "
    "node by the elapsed time of the executed task that resolved to it, the CPU floor by "
    "per-task CPU seconds / recorded cores. The two bounds share a basis, so the difference "
    "between them is a quantity a reader may reason about"
)


@dataclass(frozen=True)
class ConcurrencyFloor:
    """``max(CPU floor, critical path)`` with the binding bound named.

    The max alone is not the deliverable. A build whose floor is set by CPU
    capacity and one whose floor is set by its dependency chain want opposite
    responses - more parallelism against the first, a shorter chain against the
    second - and a bare number cannot tell them apart. So ``binding`` is a field,
    not a rendering detail, and both input bounds stay visible beside it.

    Both inputs are required. An unavailable critical path leaves this section
    unavailable rather than degrading to the CPU floor alone: a CPU-only figure
    printed under the concurrency-floor label is precisely the reading that
    overstates the achievable saving on a dependency-bound build. The same holds
    in the other direction for an unavailable CPU floor - a path-only number
    under this label is a dependency bound wearing a concurrency bound's name.

    ``headroom_seconds`` is stated against the ACTUAL build duration, and it is
    computed here rather than left for a reader to derive from the two bounds -
    see :data:`BASIS_NOTE`. It can legitimately be negative: the floor is a
    bound over the tasks a build cannot avoid running in sequence, and nothing
    guarantees the actual build stayed at or below it.
    """

    available: bool = False
    seconds: float | None = None
    binding: str | None = None
    cpu_seconds: float | None = None
    path_seconds: float | None = None
    actual_seconds: float | None = None
    headroom_seconds: float | None = None
    headroom_pct: float | None = None
    note: str = "concurrency floor unavailable: no buildstats source supplied"
    basis_note: str | None = None
    headroom_note: str | None = None

    def report_lines(self) -> list[str]:
        """Render this section as plain text lines.

        An unavailable floor renders its note and nothing else - in particular
        no bound value and no headroom, since a headroom against a floor that
        was refused is the same "took the number, dropped the caveat" failure
        :meth:`bakar.insights_joins.BuildstatsJoin.report_lines` guards.
        """
        lines = [f"  {self.note}"]
        if self.basis_note is not None:
            lines.append(f"  {self.basis_note}")
        if self.headroom_note is not None:
            lines.append(f"  {self.headroom_note}")
        return lines


@dataclass(frozen=True)
class TimingReport:
    """The timing report: top-N slowest tasks plus the critical-path section."""

    top_slowest: list[TaskDuration] = field(default_factory=list)
    critical_path: CriticalPath = field(default_factory=CriticalPath)
    regime: Regime = field(default_factory=Regime)
    graph_join: GraphJoin = field(default_factory=GraphJoin)
    buildstats_join: BuildstatsJoin = field(default_factory=BuildstatsJoin)
    cpu_floor: CpuFloor = field(default_factory=CpuFloor)
    concurrency_floor: ConcurrencyFloor = field(default_factory=ConcurrencyFloor)
    task_churn: TaskChurn = field(default_factory=TaskChurn)


def _regime_from(artifact: dict | list, task_count: int) -> Regime:
    """Read the sstate regime out of a normalized event-log artifact.

    ``task_count`` is what separates "nothing was restored" from "nothing was
    measured" - see :class:`Regime`. Passing an already-parsed ``tasks`` list
    rather than the whole artifact leaves no block to read, which reports as
    unmeasured rather than inventing a cold-build claim.
    """
    if not isinstance(artifact, dict):
        return Regime(note="regime unknown: no event-log artifact (tasks list only)")
    block = artifact.get("setscene")
    if not isinstance(block, dict):
        return Regime(note="regime unknown: artifact carries no setscene block")

    def _count(key: str) -> int:
        value = block.get(key, 0)
        return value if isinstance(value, int) else 0

    covered, notcovered, total = _count("covered"), _count("notcovered"), _count("total")
    if covered == 0 and notcovered == 0 and total == 0 and task_count == 0:
        # The eventlog early-return shape: zeros AND no tasks. Reporting this as
        # a cold build would record an unmeasured run as a measured one, on the
        # success path, with nothing warning.
        return Regime(note="regime unknown: no tasks recorded, so the zeros are unmeasured rather than cold")

    pct = (100.0 * covered / total) if total else 0.0
    label = "cold" if covered == 0 else f"{pct:.1f}% restored"
    return Regime(
        measured=True,
        covered=covered,
        notcovered=notcovered,
        total=total,
        note=f"{label} ({covered} of {total} tasks from sstate)",
    )


def _recorded_cores(artifact: dict | list) -> tuple[int | None, dict[str, int]]:
    """Return the recorded core count and any other recorded divisor candidates.

    The count is ``None`` for an artifact written before schema 5 recorded a
    ``host`` block, for a bare tasks list with no artifact to read, for an
    artifact whose caller nulled the block because it could only be synthesized
    on the analysing host (see ``commands.insights._load_artifact``), and for a
    block whose ``cpu_count`` is absent or not a positive int. Every one of
    those means "not recorded", and none of them may fall back to
    ``os.cpu_count()`` here - see :class:`CpuFloor`.
    """
    if not isinstance(artifact, dict):
        return None, {}
    block = artifact.get("host")
    if not isinstance(block, dict):
        return None, {}

    def _positive(key: str) -> int | None:
        value = block.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None

    alternates = {key: value for key in ("bb_number_threads", "parallel_make") if (value := _positive(key)) is not None}
    return _positive("cpu_count"), alternates


def _compute_cpu_floor(join: BuildstatsJoin, artifact: dict | list) -> CpuFloor:
    """Divide the joined CPU seconds by the RECORDED build-host core count.

    Degrades with a note rather than raising, and never substitutes a divisor:
    a missing join, a refused join, and a missing recorded count each produce an
    unavailable floor. Following
    :func:`bakar.insights_critical_path._compute_critical_path`'s precedent
    exactly rather than inventing a second convention for the same situation.
    """
    if join.cpu_seconds is None:
        reason = (
            "buildstats join refused, so no CPU seconds were produced"
            if join.available
            else "no joined CPU seconds available"
        )
        return CpuFloor(note=f"CPU floor unavailable: {reason}")

    cores, alternates = _recorded_cores(artifact)
    if cores is None:
        return CpuFloor(
            note=(
                "CPU floor unavailable: this run's artifact records no build-host core count "
                "(the run predates the host block, or only the raw event log survives and a block "
                "synthesized now would describe the analysing host) - the analysing host's core "
                "count is deliberately not substituted, because that would make the same capture "
                "yield a different floor on every machine"
            ),
        )

    also = "".join(f", {key}={value} recorded" for key, value in sorted(alternates.items()))
    return CpuFloor(
        available=True,
        seconds=join.cpu_seconds / cores,
        divisor=cores,
        note=(
            f"CPU floor {join.cpu_seconds / cores:.1f}s = {join.cpu_seconds:.1f} joined CPU seconds "
            f"/ {cores} cores (build host cpu_count, recorded at capture{also})"
        ),
    )


def build_window(artifact: dict | list) -> tuple[float, float] | None:
    """Return the run's ``(started, completed)`` epoch pair from its build block.

    ``None`` when there is no block to read, when either endpoint is missing,
    unparseable or non-finite, or when the span is not positive.

    The finiteness check is not redundant beside ``completed > started``.
    ``json.loads`` accepts bare ``NaN`` and ``Infinity`` tokens, and ``inf >
    0.0`` is true - so an infinite endpoint passes the ordering test and yields
    an unbounded window, which makes :func:`bakar.buildstats.select_capture`
    accept an arbitrarily late capture and makes headroom render as ``nan``
    rather than as the refusal design D2 asks for.

    Public because the buildstats capture is selected by correlating with this
    window (see :func:`bakar.buildstats.select_capture`), and the caller that
    supplies ``buildstats_source`` is the one holding the artifact.
    """
    if not isinstance(artifact, dict):
        return None
    block = artifact.get("build")
    if not isinstance(block, dict):
        return None
    try:
        started = float(block["started"])
        completed = float(block["completed"])
    except KeyError, TypeError, ValueError:
        return None
    if not (math.isfinite(started) and math.isfinite(completed)):
        return None
    return (started, completed) if completed > started else None


def correlation_window(artifact: dict | list) -> tuple[float, float] | None:
    """Return a window for CORRELATING a buildstats capture with this run.

    Deliberately not :func:`build_window`, and the difference is not cosmetic.
    ``build_window`` answers "how long did this build take", which headroom is
    stated against; this answers "when was this build running", which is the
    only question capture selection asks. A task-derived span is a wrong answer
    to the first and a safe answer to the second, so the two must not share a
    function - see :func:`_actual_build_seconds`, whose docstring rules the
    fallback out for exactly that reason.

    Prefers the build block, then falls back to the span of the run's own task
    timestamps.
    """
    if not isinstance(artifact, dict):
        return None
    return build_window(artifact) or _window_from_tasks(artifact)


def _window_from_tasks(artifact: dict) -> tuple[float, float] | None:
    """Return the span of the run's own task timestamps.

    The build block's endpoints come from ``bb.event.BuildStarted`` and
    ``BuildCompleted``, and those events carry no time attribute - verified
    against a real captured log, where both are absent before AND after
    unpickling, so ``build.started``/``build.completed`` are structurally always
    ``None``. That predates this change, but capture correlation reads the
    window, so inheriting it would make every buildstats-derived section refuse
    on every run forever - a capability that is inert is not more honest than
    one that is wrong, it is only quieter about it.

    Task rows carry real timestamps (the timing section is computed from them),
    so their span is a true subset of the build: it starts no earlier than the
    first task and ends no later than the last. Narrower than the real build on
    both ends, which is the safe direction for a correlation window - it can
    reject a capture that genuinely belongs, and cannot accept one that does
    not.

    A non-finite timestamp is skipped rather than collected. ``min``/``max``
    propagate ``nan`` by IEEE-754 semantics, so one garbled row would make the
    whole span ``nan``, fail ``completed > started``, and silently discard the
    correlation window for the entire build - one bad row disabling every
    buildstats-derived section, with the note blaming the tree.
    """
    rows = artifact.get("tasks")
    if not isinstance(rows, list):
        return None
    starts: list[float] = []
    ends: list[float] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key, sink in (("started", starts), ("completed", ends)):
            try:
                value = float(row[key])
            except KeyError, TypeError, ValueError:
                continue
            if math.isfinite(value):
                sink.append(value)
    if not starts or not ends:
        return None
    started, completed = min(starts), max(ends)
    return (started, completed) if completed > started else None


def _actual_build_seconds(artifact: dict | list) -> float | None:
    """Return the run's real wall-clock duration from the artifact's build block.

    Headroom has to be stated against what the build actually took; deriving a
    substitute from the task rows (max completed minus min started) would
    silently answer a different question - the span of task execution, which
    excludes parsing and teardown - under the same label.
    """
    window = build_window(artifact)
    return None if window is None else window[1] - window[0]


def _compute_concurrency_floor(
    cpu_floor: CpuFloor,
    critical_path: CriticalPath,
    artifact: dict | list,
) -> ConcurrencyFloor:
    """Take ``max(CPU floor, critical path)`` and name which bound binds.

    Degrades with a note rather than raising, following
    :func:`bakar.insights_critical_path._compute_critical_path`'s precedent.
    Either bound being unavailable leaves the whole section unavailable - see
    :class:`ConcurrencyFloor` for why neither one alone may be published under
    this label.
    """
    if not cpu_floor.available or cpu_floor.seconds is None:
        return ConcurrencyFloor(
            note=(
                "concurrency floor unavailable: the CPU floor is unavailable, so "
                "max(CPU floor, critical path) has only one term - reporting the critical path "
                "alone here would present a dependency bound under a concurrency bound's name"
            ),
        )
    if not critical_path.available:
        return ConcurrencyFloor(
            note=(
                "concurrency floor unavailable: the critical path is unavailable, so which bound "
                "binds cannot be determined - a CPU-only figure read as the concurrency floor "
                "overstates the achievable saving on a dependency-bound build"
            ),
        )

    cpu_seconds = cpu_floor.seconds
    path_seconds = critical_path.total_seconds
    if not (math.isfinite(cpu_seconds) and math.isfinite(path_seconds)):
        # Last line of defence rather than the first. Both inputs are guarded at
        # their sources, but ``nan`` compares false against everything, so a
        # non-finite reaching here would pick a binding bound by accident and
        # render every figure below as ``nan`` - a number-shaped output that D2
        # requires to be a stated refusal instead.
        # Which bound is at fault is named; neither VALUE is printed. The class
        # docstring's rule is that an unavailable floor renders no bound value,
        # and a non-finite one is the last value that should be made to look
        # like a figure a reader could act on.
        culprits = " and ".join(
            label
            for label, value in (("the CPU floor", cpu_seconds), ("the critical path", path_seconds))
            if not math.isfinite(value)
        )
        return ConcurrencyFloor(
            note=(
                f"concurrency floor unavailable: {culprits} is not a finite duration, so max() would "
                "name a binding bound by accident and every figure below it would render as nan"
            ),
        )
    binding = "path" if path_seconds >= cpu_seconds else "cpu"
    seconds = max(path_seconds, cpu_seconds)

    actual = _actual_build_seconds(artifact)
    headroom = headroom_pct = None
    if actual is None:
        headroom_note = (
            "headroom unavailable: this run's artifact records no build start/finish pair, so "
            "there is no actual duration to state headroom against"
        )
    else:
        headroom = actual - seconds
        headroom_pct = 100.0 * headroom / actual
        if headroom >= 0:
            headroom_note = (
                f"headroom {headroom:.1f}s of {actual:.1f}s actual ({headroom_pct:.1f}%), against "
                f"the binding bound ({BINDING_LABELS[binding]}) rather than against the CPU floor alone"
            )
        else:
            headroom_note = (
                f"headroom negative: the floor {seconds:.1f}s exceeds the {actual:.1f}s this build "
                f"actually took ({headroom_pct:.1f}%). Both bounds are task-level (design D5), so "
                "this is a real signal rather than an artifact of over-weighting - it may reflect a "
                "dependency-chain bottleneck, measurement noise, or something else worth investigating"
            )

    return ConcurrencyFloor(
        available=True,
        seconds=seconds,
        binding=binding,
        cpu_seconds=cpu_seconds,
        path_seconds=path_seconds,
        actual_seconds=actual,
        headroom_seconds=headroom,
        headroom_pct=headroom_pct,
        note=(
            f"concurrency floor {seconds:.1f}s = max(CPU floor {cpu_seconds:.1f}s, "
            f"critical path {path_seconds:.1f}s) - {BINDING_LABELS[binding]} binds"
        ),
        basis_note=BASIS_NOTE,
        headroom_note=headroom_note,
    )


def timing_report(
    artifact: dict | list,
    top_n: int = DEFAULT_TOP_N,
    *,
    baselines_path: Path | None = None,
    dependency_source: Callable[[], tuple[str, str]] | None = None,
    buildstats_source: Callable[[], BuildstatsRun] | None = None,
) -> TimingReport:
    """Return the per-task timing report for one run.

    ``artifact`` is either a normalized ``bitbake-events.json`` dict or its
    already-parsed ``tasks`` list (per :func:`bakar.task_rollup.tasks_from`).
    A row missing ``completed`` (started-but-not-finished) or whose duration
    is negative or non-finite is skipped without raising. The returned
    ``top_slowest`` list holds exactly ``top_n`` entries when at least that many
    valid-duration tasks exist, or every valid-duration task (unpadded)
    otherwise.

    Such a row is skipped from the DURATIONS only. Its ``(recipe, task)``
    identity still joins the executed set
    (:data:`bakar.insights_joins._ExecutedTask`) that feeds the join gate and
    the churn coverage, because it names a task that ran and the gate's
    denominator is "tasks that ran", not "tasks bitbake timestamped usably".

    Baseline context comes from :func:`bakar.task_timings.load_baselines`
    (``baselines_path`` threads through to it for tests; ``None`` uses the
    default on-disk location) - this reads the existing cross-build baseline
    store rather than recomputing a second one from the raw event deltas.

    ``dependency_source``, when supplied, is called with no arguments and
    must return ``(dot_text, buildlist_text)`` for the critical-path section
    (see :func:`bakar.insights_critical_path._compute_critical_path`).
    Omitting it (the default) leaves ``critical_path`` at its "unavailable,
    not requested" default; a failure inside the callable or the resulting
    graph degrades to an explicit "unavailable" result rather than raising or
    dropping the duration/top-N sections computed above. The same capture
    feeds the :class:`~bakar.insights_joins.GraphJoin` gate - the share of
    executed tasks resolving to a graph node - and the path publishes only
    when that gate passes, so a chain over a partially joined graph has no
    expressible form here. The source is called once for both.

    ``buildstats_source``, when supplied, is called with no arguments and must
    return a :class:`bakar.buildstats.BuildstatsRun`. It feeds the join gate
    (see :class:`~bakar.insights_joins.BuildstatsJoin`): the share of executed
    tasks carrying a buildstats record, and the refusal that keeps every
    CPU-derived figure out of the report when that share falls below
    :data:`~bakar.insights_joins.JOIN_RATE_THRESHOLD`. It degrades the same way
    ``dependency_source`` does. When the gate passes, the :class:`CpuFloor`
    section divides those CPU seconds by the core count ``artifact`` recorded
    on the BUILD host - no divisor is read from the analysing host, so the
    same artifact yields the same floor anywhere.

    The :class:`ConcurrencyFloor` section combines that floor with the
    critical path as ``max(CPU floor, critical path)`` and names which of the
    two binds. It therefore needs BOTH ``dependency_source`` and
    ``buildstats_source``; with either omitted or degraded it reports
    unavailable rather than publishing the surviving bound under its label.

    The :class:`~bakar.insights_churn.TaskChurn` section aggregates the same
    records' fault, syscall and write counters per task type. It needs only
    ``buildstats_source``, and unlike the floor it is not withheld on a
    refused join - see :class:`~bakar.insights_churn.TaskChurn` for why a
    per-task-type aggregate survives a partial coverage that a build-wide
    bound does not.
    """
    baselines = task_timings.load_baselines(baselines_path)

    # ``tasks_from`` only handles a path or an already-parsed ``tasks`` list
    # (a dict artifact isn't Path/str/list, so passing it straight through
    # raises); unwrap the artifact's ``tasks`` key first, then let
    # ``tasks_from`` do the list-verbatim/path-read extraction.
    tasks_source = artifact.get("tasks", []) if isinstance(artifact, dict) else artifact

    durations: list[TaskDuration] = []
    executed: list[_ExecutedTask] = []
    for row in tasks_from(tasks_source):
        if not isinstance(row, dict):
            continue
        task = row.get("task")
        recipe = row.get("recipe")
        started = row.get("started")
        completed = row.get("completed")
        if not isinstance(task, str):
            continue
        recipe_name = recipe if isinstance(recipe, str) else ""

        # Identity first, and deliberately BEFORE every timestamp guard below.
        # ``executed`` is the join's denominator and ``durations`` is the input
        # to the wall-clock and critical-path work, and those are different
        # questions: a task with no usable timestamp still ran, and dropping it
        # from both the numerator and the denominator pinned the rate at 100%
        # and passed the gate vacuously over exactly the partial-coverage case
        # the gate exists to catch. Membership is decided by ``outcome`` alone
        # (see :data:`bakar.insights_joins._ExecutedTask`).
        if row.get("outcome") != _NOT_EXECUTED_OUTCOME:
            executed.append((recipe_name, task))

        # Defense in depth: a failed_silent row normally carries no `started`
        # (see :data:`bakar.insights_joins._ExecutedTask`) and is dropped by
        # the guard below anyway. A malformed or legacy artifact could violate
        # that invariant and still carry a timestamp pair; excluding the
        # outcome explicitly here keeps `durations` - and therefore every node
        # weight derived from it - free of a task the join gate's own
        # denominator (`executed`) never counted.
        if row.get("outcome") == _NOT_EXECUTED_OUTCOME:
            continue

        if started is None or completed is None:
            continue
        try:
            duration = float(completed) - float(started)
        except TypeError, ValueError:
            continue
        if not math.isfinite(duration) or duration < 0:
            continue

        baseline = baselines.get(task_timings.baseline_key(recipe_name, task))
        mean, stddev = baseline if baseline is not None else (None, None)

        durations.append(
            TaskDuration(
                recipe=recipe_name,
                task=task,
                duration=duration,
                baseline_mean=mean,
                baseline_stddev=stddev,
            )
        )

    durations.sort(key=lambda d: d.duration, reverse=True)
    top_slowest = durations[:top_n] if top_n >= 0 else list(durations)

    critical_path = CriticalPath()
    graph_join = GraphJoin()
    if dependency_source is not None:
        # One parse, two consumers. The join measures coverage and owns the
        # gate; the path reads that verdict rather than re-deriving one, and
        # neither calls the source again - ``_dependency_source(run_dir,
        # window)`` correlates the capture against the build window, so a
        # second invocation is not free.
        parsed = _parse_dependency_graph(dependency_source)
        graph_join = _compute_graph_join(parsed, executed)
        critical_path = _compute_critical_path(parsed, durations, graph_join)

    buildstats_join = BuildstatsJoin()
    cpu_floor = CpuFloor()
    task_churn = TaskChurn()
    if buildstats_source is not None:
        # Two sections read the same tree. Caching the zero-argument call keeps
        # the directory walk to one pass without either section having to know
        # the other exists; a raising source is not cached, so both still see
        # the failure and both still degrade on it.
        cached_source = functools.cache(buildstats_source)
        buildstats_join = _compute_join(cached_source, executed)
        cpu_floor = _compute_cpu_floor(buildstats_join, artifact)
        task_churn = _compute_churn(cached_source, executed)

    return TimingReport(
        top_slowest=top_slowest,
        critical_path=critical_path,
        regime=_regime_from(artifact, len(durations)),
        graph_join=graph_join,
        buildstats_join=buildstats_join,
        cpu_floor=cpu_floor,
        concurrency_floor=_compute_concurrency_floor(cpu_floor, critical_path, artifact),
        task_churn=task_churn,
    )
