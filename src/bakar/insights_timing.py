"""Per-task timing and top-N-slowest report.

bitbake records per-task ``started``/``completed`` timestamps in the
normalized ``bitbake-events.json`` artifact (see :mod:`bakar.eventlog`).
:func:`timing_report` turns those rows into a ranked "where did my build time
go" view: the top-N slowest individual tasks, each annotated with the prior
cross-build mean/stddev already tracked by :mod:`bakar.task_timings` (no
second baseline store is built here).

The tasks-list extraction and missing/negative-duration guard reuse
:func:`bakar.task_rollup._tasks_from` rather than re-parsing the ``tasks``
list a third time (see design.md's "reuse ``_tasks_from``" decision).

This module also exposes an optional critical-path sub-section: the longest
dependency-respecting serial chain through the build, weighted by this run's
per-recipe task durations. Per design.md's confirmed finding that
``commands/graph.py``'s dependency model always invokes ``bitbake -g
<recipe>`` live inside kas-container (no cached/offline model exists), the
critical-path step cannot be a pure function over the persisted artifact
alone. It is opt-in: callers pass a ``dependency_source`` callable that
returns the ``(dot_text, buildlist_text)`` pair (however they were
retrieved - live container exec in production, a canned fixture in tests).
When ``dependency_source`` is omitted, or it raises, or the resulting graph
is empty/cyclic, :class:`CriticalPath` reports ``available=False`` with an
explanatory ``note`` - the duration and top-N-slowest sections above never
depend on this section's success.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import networkx as nx

from bakar import graph_analyze, task_timings
from bakar.task_rollup import _tasks_from

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from bakar.buildstats import BuildstatsRun, TaskStats

DEFAULT_TOP_N = 10

#: Share of executed tasks that must carry a buildstats record before any
#: CPU figure derived from that join may be published. The reference analyser
#: (chezmoi's ``yocto-bench-buildstats.py``) arrived at the same 95% after
#: publishing a path over a partially-joined graph: the result was confidently
#: wrong and read exactly like a correct one.
JOIN_RATE_THRESHOLD = 0.95

#: How many unjoined task keys a refusal names. Enough to recognise a pattern
#: (all setscene, all one recipe) without pasting the whole shortfall.
UNJOINED_SAMPLE = 5

#: Column widths for the churn table, in the order the header names them. Held
#: as a constant rather than as formatter arguments because this project is
#: mid-way through a high-arity cleanup and a per-column parameter list is
#: exactly the signature that pass adds back.
#:
#: They sum to 79 plus a leading space, which keeps a row inside an 80-column
#: terminal, and the first column holds the longest real task name
#: (``do_package_write_rpm_setscene``, 29). A wider table is not a cosmetic
#: problem: Rich hard-wraps the overflow onto a second line, and a row split
#: across two lines is exactly the shape that gets read against the wrong
#: column heading.
CHURN_COLUMNS = ("task type", "tasks", "minflt", "majflt", "syscalls", "GB_wr")
CHURN_WIDTHS = (30, 5, 14, 10, 12, 8)

#: Bytes per gigabyte for the ``GB_wr`` column. Decimal, matching how the
#: reference analyser labelled the same column.
BYTES_PER_GB = 1_000_000_000


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
class CriticalPath:
    """The critical-path sub-section: the longest dependency-respecting chain.

    ``available`` is ``False`` (the default) when no dependency source was
    supplied to :func:`timing_report`, or when the supplied source failed,
    returned an empty graph, or returned a cyclic graph - in every one of
    those cases ``note`` explains why, and ``chain``/``total_seconds`` stay
    at their empty defaults. The duration and top-N sections of
    :class:`TimingReport` never depend on this section's state.
    """

    available: bool = False
    chain: list[str] = field(default_factory=list)
    total_seconds: float = 0.0
    note: str = "critical-path unavailable"


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
class BuildstatsJoin:
    """How much of this run's executed task set carries a buildstats record.

    Every CPU-derived figure in this report - the CPU floor, and therefore the
    concurrency floor built on it - is computed over the tasks that joined. A
    join covering 60% of the build yields a floor that is arithmetically fine
    and factually a floor for a different, smaller build, with nothing in its
    formatting to say so. So the rate is the gate, not a footnote.

    ``cpu_seconds`` is ``None`` whenever ``gate_passed`` is false. That is
    structural rather than stylistic: a downstream section cannot print a floor
    it was never handed the input for, so the "refused but printed anyway"
    failure has no expressible form here.

    ``joined`` and ``executed`` are counted over the SAME set - every executed
    task raises ``executed``, and only a task with a matching buildstats record
    also raises ``joined``. Dropping unjoined tasks from both sides instead
    would pin the rate at 100% and pass the gate vacuously over precisely the
    partial-join case it exists to catch.

    ``available`` says the join was computed at all; it is false when the
    buildstats tree was absent, empty, or its source raised. Those are distinct
    from a computed-but-failing rate, which is ``available=True,
    gate_passed=False``.
    """

    available: bool = False
    gate_passed: bool = False
    executed: int = 0
    joined: int = 0
    cpu_seconds: float | None = None
    unjoined_sample: list[str] = field(default_factory=list)
    note: str = "buildstats join unavailable: no buildstats source supplied"

    @property
    def rate(self) -> float:
        """Joined share of executed tasks, 0.0 when nothing executed.

        Zero executed tasks is a refusal, not a pass: a rate defined as 1.0 over
        an empty denominator would clear the gate on a run that measured nothing.
        """
        return (self.joined / self.executed) if self.executed else 0.0

    def report_lines(self) -> list[str]:
        """Render this section as plain text lines.

        Nothing here formats a duration, and that is the invariant under test:
        when the gate refuses there is no "would have been" figure, no debug
        field and no parenthetical carrying seconds. A refused floor that still
        shows its number is the failure this whole section exists to prevent -
        a reader takes the number and discards the caveat.
        """
        lines = [f"  {self.note}"]
        lines.extend(f"  unjoined: {name}" for name in self.unjoined_sample)
        return lines


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
    an explicit note, following :func:`_compute_critical_path`'s precedent -
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

#: Stated beside every available concurrency floor. Design D3 keeps the path
#: recipe-level and the CPU floor task-level, and deliberately does NOT convert
#: one to the other - so the two bounds are measured differently and the
#: difference between them is not a meaningful quantity. Saying so is the
#: mitigation D3's own risk table names.
BASIS_NOTE = (
    "basis: the critical path is recipe-level - each node carries that recipe's summed task "
    "seconds, including tasks not themselves on the path - while the CPU floor is task-level "
    "(per-task CPU seconds / recorded cores). Per design D3 the two are not converted to a "
    "common basis: the headroom below is computed here against the binding bound, and the "
    "difference between the two bounds is not a quantity to subtract"
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
    see :data:`BASIS_NOTE`. It can legitimately be negative: the recipe-level
    path over-weights by construction (design D3), so a path exceeding the real
    build is evidence the path is an over-estimate, not that the build beat its
    own floor.
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
        :meth:`BuildstatsJoin.report_lines` guards.
        """
        lines = [f"  {self.note}"]
        if self.basis_note is not None:
            lines.append(f"  {self.basis_note}")
        if self.headroom_note is not None:
            lines.append(f"  {self.headroom_note}")
        return lines


#: Stated beside every available churn table. Two things a reader cannot
#: recover from the numbers themselves: which rusage the counters come from,
#: and that the columns are read by field name.
#:
#: The self/child split is not a detail. On one real capture ``do_configure``
#: read 7,023,117 self minor faults against 214,243,919 CHILD minor faults -
#: the child accounts for 97% of the churn, because the work happens in spawned
#: configure and compiler subprocesses. A table summing only ``rusage ru_*``
#: under-reports by roughly 30x on exactly the task types this section exists
#: to characterize, so the choice is stated rather than left to be inferred.
CHURN_BASIS_NOTE = (
    "basis: each counter sums the task's OWN rusage and its CHILD rusage "
    "(bitbake forks the real work out, and the child carries ~97% of the faults on a real "
    "capture, so self-only counters under-report by roughly 30x). Fields are read by name from "
    "each buildstats file - 'rusage ru_minflt', 'Child rusage ru_majflt', 'IO syscr'/'IO syscw', "
    "'IO write_bytes' - never by column position"
)


def _churn_line(cells: tuple[str, ...]) -> str:
    """Lay one churn row - or the header - out on :data:`CHURN_WIDTHS`.

    The header and every data row go through this one function, so the two
    cannot drift into disagreeing about which column is which. The first column
    is left-justified and the numeric ones right-justified: right-justifying a
    task name would run it up against the ``tasks`` heading with no gap, which
    is how a reader ends up parsing a value against its neighbour's label.
    """
    parts = []
    for index, (cell, width) in enumerate(zip(cells, CHURN_WIDTHS, strict=True)):
        parts.append(cell.ljust(width) if index == 0 else cell.rjust(width))
    return "".join(parts)


@dataclass(frozen=True)
class ChurnRow:
    """One task type's summed process-churn and I/O counters.

    ``minflt`` is process churn - pages faulted in without touching the disk,
    which is what a storm of short-lived autoconf probe processes produces.
    ``majflt`` and ``write_bytes`` are I/O. Keeping them in separate columns is
    the whole capability: the two profiles were indistinguishable on wall-clock
    alone, and it was the minor-to-major ratio that identified probe churn as a
    serial floor rather than a disk problem.
    """

    task: str
    tasks: int
    minflt: int
    majflt: int
    syscalls: int
    write_bytes: int


@dataclass(frozen=True)
class TaskChurn:
    """Per-task-type churn columns aggregated over the joined buildstats records.

    Only records matching an executed task contribute, for the same reason
    :func:`_compute_join` restricts its CPU sum: a buildstats tree can carry rows
    from a previous run or a sibling machine's directory, and summing the tree
    wholesale credits them to this build.

    Unlike the CPU floor, this section is NOT withheld when the join gate
    refuses. The floor is a single build-wide bound, so a partial join makes it a
    bound for a different, smaller build; these are per-task-type aggregates, and
    a subset of ``do_compile`` records still describes ``do_compile``. What a
    partial join does cost is coverage, so ``covered``/``executed`` are stated in
    the note on every rendering rather than only on a refusal.
    """

    available: bool = False
    rows: list[ChurnRow] = field(default_factory=list)
    covered: int = 0
    executed: int = 0
    note: str = "task churn unavailable: no buildstats source supplied"
    basis_note: str | None = None

    def report_lines(self) -> list[str]:
        """Render the note, the basis, and the column table.

        Columns are emitted in :data:`CHURN_COLUMNS` order with the header
        printed from the same constant, so a reader and the formatter cannot
        disagree about which column is which - the failure this task's own
        history names, where the ``majflt`` column was quoted as a task count
        and ``GB_wr`` as cores-per-task.
        """
        lines = [f"  {self.note}"]
        if self.basis_note is not None:
            lines.append(f"  {self.basis_note}")
        if not self.available:
            return lines
        lines.append(" " + _churn_line(CHURN_COLUMNS))
        for row in self.rows:
            cells = (
                row.task,
                f"{row.tasks}",
                f"{row.minflt:,}",
                f"{row.majflt:,}",
                f"{row.syscalls:,}",
                f"{row.write_bytes / BYTES_PER_GB:.2f}",
            )
            lines.append(" " + _churn_line(cells))
        return lines


@dataclass(frozen=True)
class TimingReport:
    """The timing report: top-N slowest tasks plus the critical-path section."""

    top_slowest: list[TaskDuration] = field(default_factory=list)
    critical_path: CriticalPath = field(default_factory=CriticalPath)
    regime: Regime = field(default_factory=Regime)
    buildstats_join: BuildstatsJoin = field(default_factory=BuildstatsJoin)
    cpu_floor: CpuFloor = field(default_factory=CpuFloor)
    concurrency_floor: ConcurrencyFloor = field(default_factory=ConcurrencyFloor)
    task_churn: TaskChurn = field(default_factory=TaskChurn)


def _duration_totals(durations: list[TaskDuration]) -> dict[str, float]:
    """Sum durations per recipe (PN) across all of that recipe's tasks.

    The dependency graph is PN-level (:func:`bakar.graph_analyze.collapse_to_pn`
    strips each node to its bare package name), while ``TaskDuration.recipe``
    carries the full versioned PF (e.g. ``busybox-1.36.1-r0``) straight from
    the event log. Keying this dict on the raw PF would never match a PN graph
    node, silently zeroing every critical-path edge weight - strip the version
    the same way :func:`bakar.task_timings.strip_recipe_version` does for
    baseline keys, so both sides share one namespace.
    """
    totals: dict[str, float] = {}
    for d in durations:
        pn = task_timings.strip_recipe_version(d.recipe)
        totals[pn] = totals.get(pn, 0.0) + d.duration
    return totals


def _weighted_longest_path(graph: nx.DiGraph, node_weights: dict[str, float]) -> tuple[list[str], float]:
    """Return the node chain and total weight of the heaviest path through ``graph``.

    ``nx.dag_longest_path(weight=...)`` sums EDGE weights, so a path's first
    node - which has no incoming edge - never contributes its own weight to
    the comparison. That silently favors a path whose head node has a large
    duration less than it should, and can pick the wrong chain entirely (a
    two-node chain A->B with duration(A)=100, duration(B)=1 loses to an
    unrelated C->D with duration(C)=10, duration(D)=50, because only B's and
    D's durations ever reach an edge weight). This does the standard DAG
    longest-path DP with weight on NODES instead: ``best[v] = node_weights[v]
    + max(best[u] for u in predecessors(v), default=0)``, so every node's own
    duration counts once, including the chain's head.
    """
    order = list(nx.topological_sort(graph))
    best: dict[str, float] = {}
    predecessor: dict[str, str | None] = {}
    for node in order:
        preds = list(graph.predecessors(node))
        if preds:
            best_pred = max(preds, key=lambda p: best[p])
            best[node] = best[best_pred] + node_weights.get(node, 0.0)
            predecessor[node] = best_pred
        else:
            best[node] = node_weights.get(node, 0.0)
            predecessor[node] = None

    end_node = max(best, key=lambda n: best[n])
    chain: list[str] = []
    cur: str | None = end_node
    while cur is not None:
        chain.append(cur)
        cur = predecessor[cur]
    chain.reverse()
    return chain, best[end_node]


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


def _compute_critical_path(
    dependency_source: Callable[[], tuple[str, str]],
    duration_totals: dict[str, float],
) -> CriticalPath:
    """Compute the duration-weighted critical path from a dependency source.

    ``dependency_source`` returns ``(dot_text, buildlist_text)`` - the same
    two artifacts ``bakar graph`` retrieves from a live ``bitbake -g`` run
    (see :mod:`bakar.commands.graph`). Parsing reuses
    :func:`bakar.graph_analyze.read_graph`/``collapse_to_pn`` instead of
    re-implementing DOT parsing.

    Any failure - the callable raises, the graph is empty, or it is cyclic -
    degrades to an explicit "unavailable" :class:`CriticalPath` with a note;
    this function never raises back to :func:`timing_report`.
    """
    try:
        # buildlist_text (package_count etc.) isn't needed for the chain itself.
        dot_text, _buildlist_text = dependency_source()
        pn_graph = graph_analyze.collapse_to_pn(graph_analyze.read_graph(dot_text))
    except Exception as exc:  # noqa: BLE001 - any dependency-source failure degrades gracefully
        return CriticalPath(note=f"critical-path unavailable: dependency source failed ({exc})")

    if pn_graph.number_of_nodes() == 0:
        return CriticalPath(note="critical-path unavailable: empty dependency graph")
    if not nx.is_directed_acyclic_graph(pn_graph):
        return CriticalPath(note="critical-path unavailable: cyclic dependency graph")

    chain, total = _weighted_longest_path(pn_graph, duration_totals)
    return CriticalPath(available=True, chain=chain, total_seconds=total, note="critical-path computed")


def _join_key(recipe: str, task: str) -> tuple[str, str]:
    """Key both sides of the join on ``(PN, task)``, version stripped.

    The event log records a versioned PF (``busybox-1.36.1-r0``) and so does the
    buildstats recipe directory, but they are not guaranteed to agree on the
    revision suffix - a task restored from sstate and one rebuilt after a bump
    can disagree by ``-r0`` alone. Stripping the version the way
    :func:`bakar.task_timings.strip_recipe_version` already does for baseline
    keys lets those two spellings still meet.

    This key is a FALLBACK, never the primary one - see :func:`_match_record`.
    Aggregating records under it would merge ``busybox-1.36.1-r0`` and
    ``busybox-1.37-r0`` into one bucket, so a stale record for a PF this run
    never executed would be credited to the PF it did.
    """
    return (task_timings.strip_recipe_version(recipe), task)


_RecordKey = tuple[str, str]


def _index_records(
    tasks: list[TaskStats],
) -> tuple[dict[_RecordKey, list[TaskStats]], dict[_RecordKey, list[_RecordKey]]]:
    """Index buildstats records by exact ``(PF, task)`` and by stripped key.

    The second index maps a stripped key to every exact key carrying it, which
    is what makes an ambiguous strip detectable rather than silently merged.
    """
    exact: dict[tuple[str, str], list[TaskStats]] = {}
    stripped: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for stat in tasks:
        key = (stat.recipe, stat.task)
        exact.setdefault(key, []).append(stat)
        bucket = stripped.setdefault(_join_key(stat.recipe, stat.task), [])
        if key not in bucket:
            bucket.append(key)
    return exact, stripped


def _match_record(
    exact: dict[tuple[str, str], list[TaskStats]],
    stripped: dict[tuple[str, str], list[tuple[str, str]]],
    recipe: str,
    task: str,
) -> tuple[str, str] | None:
    """Resolve one executed task to the buildstats record it owns, or ``None``.

    Exact ``(PF, task)`` first, because that is the only match that proves the
    record belongs to the version this run executed. The version-stripped key is
    tried next and ONLY when it is unambiguous, which is what keeps the strip
    doing the job it was added for - the two sides disagreeing by a revision
    suffix - without letting it credit a version the run never built.

    An ambiguous stripped key resolves to ``None`` and therefore counts as
    unjoined. That lowers the join rate, which is the honest signal: the tree
    holds two versions of this recipe and nothing here can say which one ran.
    """
    key = (recipe, task)
    if key in exact:
        return key
    candidates = stripped.get(_join_key(recipe, task), ())
    return candidates[0] if len(candidates) == 1 else None


def _capture_phrase(run: BuildstatsRun) -> str:
    """Name the capture directory the figures came from.

    Printed on the PASSING path as well as the refusing ones. Naming the source
    only when the section declines to publish is exactly backwards for auditing:
    the number a reader might act on is the one whose provenance they need.
    """
    return "capture directory unrecorded" if run.directory is None else f"from capture {run.directory}"


def _compute_join(
    buildstats_source: Callable[[], BuildstatsRun],
    durations: list[TaskDuration],
) -> BuildstatsJoin:
    """Join executed tasks against buildstats records and gate on the rate.

    Follows :func:`_compute_critical_path`'s precedent exactly: any failure -
    the callable raises, the tree is absent, the tree is empty, no capture
    correlates with this run - returns an explicit unavailable result with a
    note and never raises back to :func:`timing_report`.

    The four outcomes keep separate notes. They are what
    :mod:`bakar.buildstats` went out of its way to distinguish, and collapsing
    them here would put the distinction back in the bin it was lifted out of:
    a tree that was never found is a path problem, a tree that recorded nothing
    is a measurement, and a tree holding only some other build's captures is a
    provenance failure that a rate near 100% would otherwise hide.
    """
    try:
        run = buildstats_source()
    except Exception as exc:  # noqa: BLE001 - any buildstats-source failure degrades gracefully
        return BuildstatsJoin(note=f"buildstats join unavailable: source failed ({exc})")

    if run.outcome == "absent":
        return BuildstatsJoin(note=f"buildstats join unavailable: tree absent ({run.note})")
    if run.outcome == "uncorrelated":
        return BuildstatsJoin(note=f"buildstats join unavailable: no capture belongs to this run ({run.note})")
    if run.outcome != "parsed":
        return BuildstatsJoin(
            note=f"buildstats join unavailable: tree present but recorded nothing ({run.note})",
        )

    exact, stripped = _index_records(run.tasks)

    matched: set[tuple[str, str]] = set()
    unjoined: list[str] = []
    joined = 0
    for d in durations:
        key = _match_record(exact, stripped, d.recipe, d.task)
        if key is None:
            unjoined.append(f"{d.recipe}:{d.task}")
        else:
            joined += 1
            matched.add(key)

    executed = len(durations)
    if not executed:
        return BuildstatsJoin(available=True, note="buildstats join refused: no executed tasks to join against")

    rate_pct = 100.0 * joined / executed
    gate_pct = 100.0 * JOIN_RATE_THRESHOLD
    if joined < JOIN_RATE_THRESHOLD * executed:
        return BuildstatsJoin(
            available=True,
            executed=executed,
            joined=joined,
            unjoined_sample=unjoined[:UNJOINED_SAMPLE],
            note=(
                f"buildstats join refused: {rate_pct:.1f}% of executed tasks joined, below the "
                f"{gate_pct:.1f}% gate ({executed - joined} of {executed} executed tasks have no "
                f"buildstats record) - no CPU-derived figure is reported for this run. "
                f"{_capture_phrase(run)}"
            ),
        )

    # Only records an executed task actually matched contribute, and matching is
    # by exact ``(PF, task)`` with an unambiguous version-stripped fallback (see
    # :func:`_match_record`). A buildstats tree carries rows this run never
    # executed - a stale capture, a second version of the same recipe, a sibling
    # machine's directory - and neither summing the tree wholesale nor
    # aggregating under the stripped key would keep those out of the total.
    return BuildstatsJoin(
        available=True,
        gate_passed=True,
        executed=executed,
        joined=joined,
        cpu_seconds=sum(stat.cpu_seconds for k in matched for stat in exact[k]),
        note=(
            f"buildstats join {rate_pct:.1f}% ({joined} of {executed} executed tasks matched a "
            f"buildstats record). {_capture_phrase(run)}"
        ),
    )


def _recorded_cores(artifact: dict | list) -> tuple[int | None, dict[str, int]]:
    """Return the recorded core count and any other recorded divisor candidates.

    The count is ``None`` for an artifact written before schema 5 recorded a
    ``host`` block, for a bare tasks list with no artifact to read, and for a
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
    unavailable floor. Following :func:`_compute_critical_path`'s precedent
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
                "(written before the host block existed) - the analysing host's core count is "
                "deliberately not substituted, because that would make the same capture yield a "
                "different floor on every machine"
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

    ``None`` when there is no block to read, when either endpoint is missing or
    unparseable, or when the span is not positive.

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
                sink.append(float(row[key]))
            except KeyError, TypeError, ValueError:
                continue
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
    :func:`_compute_critical_path`'s precedent. Either bound being unavailable
    leaves the whole section unavailable - see :class:`ConcurrencyFloor` for why
    neither one alone may be published under this label.
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
                f"actually took ({headroom_pct:.1f}%). The recipe-level path over-weights by "
                "construction (design D3), so read this as the path being an over-estimate rather "
                "than as the build beating its own floor"
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


def _compute_churn(
    buildstats_source: Callable[[], BuildstatsRun],
    durations: list[TaskDuration],
) -> TaskChurn:
    """Aggregate churn counters per task type over the executed task set.

    Degrades with a note rather than raising, following
    :func:`_compute_critical_path`'s precedent, and keeps ``absent``, ``empty``
    and ``uncorrelated`` apart for the reason :func:`_compute_join` does.

    Records are resolved through :func:`_match_record`, the same matcher the
    join gate uses, so a record for a PF this run never executed reaches no
    counter here either. Walking the tree and testing the version-stripped key
    instead credits a stale ``busybox-1.37-r0`` row to the ``busybox-1.36.1-r0``
    the run actually built, which inflates every counter in the row and reports
    a task count higher than the number of tasks that ran.

    Rows are ordered by minor faults descending, which puts the process-churn
    heavy task types at the top - the ordering that made the ``do_configure``
    profile visible in the first place.
    """
    try:
        run = buildstats_source()
    except Exception as exc:  # noqa: BLE001 - any buildstats-source failure degrades gracefully
        return TaskChurn(note=f"task churn unavailable: source failed ({exc})")

    if run.outcome == "absent":
        return TaskChurn(note=f"task churn unavailable: tree absent ({run.note})")
    if run.outcome == "uncorrelated":
        return TaskChurn(note=f"task churn unavailable: no capture belongs to this run ({run.note})")
    if run.outcome != "parsed":
        return TaskChurn(note=f"task churn unavailable: tree present but recorded nothing ({run.note})")

    exact, stripped = _index_records(run.tasks)
    # Distinct executed tasks covered, not records aggregated. Several executed
    # rows can resolve to one record key, so a record count would let ``covered``
    # exceed ``executed`` and turn the coverage note into a claim nobody can read.
    matched: set[tuple[str, str]] = set()
    for d in durations:
        key = _match_record(exact, stripped, d.recipe, d.task)
        if key is not None:
            matched.add(key)

    grouped: dict[str, list[TaskStats]] = {}
    for key in matched:
        for stat in exact[key]:
            grouped.setdefault(stat.task, []).append(stat)

    if not grouped:
        return TaskChurn(
            note=(
                f"task churn unavailable: none of the {len(run.tasks)} buildstats records match an "
                "executed task, so every counter would describe a different build"
            ),
        )

    rows = [
        ChurnRow(
            task=task,
            tasks=len(stats),
            minflt=sum(s.minflt for s in stats),
            majflt=sum(s.majflt for s in stats),
            syscalls=sum(s.syscalls for s in stats),
            write_bytes=sum(s.write_bytes for s in stats),
        )
        for task, stats in grouped.items()
    ]
    rows.sort(key=lambda r: r.minflt, reverse=True)
    return TaskChurn(
        available=True,
        rows=rows,
        covered=len(matched),
        executed=len(durations),
        note=(
            f"task churn over {len(matched)} of {len(durations)} executed tasks, aggregated into "
            f"{len(rows)} task types, {_capture_phrase(run)}"
        ),
        basis_note=CHURN_BASIS_NOTE,
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
    already-parsed ``tasks`` list (per :func:`bakar.task_rollup._tasks_from`).
    A row missing ``completed`` (started-but-not-finished) or whose duration
    is negative is skipped without raising. The returned ``top_slowest`` list
    holds exactly ``top_n`` entries when at least that many valid-duration
    tasks exist, or every valid-duration task (unpadded) otherwise.

    Baseline context comes from :func:`bakar.task_timings.load_baselines`
    (``baselines_path`` threads through to it for tests; ``None`` uses the
    default on-disk location) - this reads the existing cross-build baseline
    store rather than recomputing a second one from the raw event deltas.

    ``dependency_source``, when supplied, is called with no arguments and
    must return ``(dot_text, buildlist_text)`` for the critical-path section
    (see :func:`_compute_critical_path`). Omitting it (the default) leaves
    ``critical_path`` at its "unavailable, not requested" default; a failure
    inside the callable or the resulting graph degrades to an explicit
    "unavailable" result rather than raising or dropping the duration/top-N
    sections computed above.

    ``buildstats_source``, when supplied, is called with no arguments and must
    return a :class:`bakar.buildstats.BuildstatsRun`. It feeds the join gate
    (see :class:`BuildstatsJoin`): the share of executed tasks carrying a
    buildstats record, and the refusal that keeps every CPU-derived figure out
    of the report when that share falls below :data:`JOIN_RATE_THRESHOLD`. It
    degrades the same way ``dependency_source`` does. When the gate passes, the
    :class:`CpuFloor` section divides those CPU seconds by the core count
    ``artifact`` recorded on the BUILD host - no divisor is read from the
    analysing host, so the same artifact yields the same floor anywhere.

    The :class:`ConcurrencyFloor` section combines that floor with the
    critical path as ``max(CPU floor, critical path)`` and names which of the
    two binds. It therefore needs BOTH ``dependency_source`` and
    ``buildstats_source``; with either omitted or degraded it reports
    unavailable rather than publishing the surviving bound under its label.

    The :class:`TaskChurn` section aggregates the same records' fault, syscall
    and write counters per task type. It needs only ``buildstats_source``, and
    unlike the floor it is not withheld on a refused join - see
    :class:`TaskChurn` for why a per-task-type aggregate survives a partial
    coverage that a build-wide bound does not.
    """
    baselines = task_timings.load_baselines(baselines_path)

    # ``_tasks_from`` only handles a path or an already-parsed ``tasks`` list
    # (a dict artifact isn't Path/str/list, so passing it straight through
    # raises); unwrap the artifact's ``tasks`` key first, then let
    # ``_tasks_from`` do the list-verbatim/path-read extraction.
    tasks_source = artifact.get("tasks", []) if isinstance(artifact, dict) else artifact

    durations: list[TaskDuration] = []
    for row in _tasks_from(tasks_source):
        if not isinstance(row, dict):
            continue
        task = row.get("task")
        recipe = row.get("recipe")
        started = row.get("started")
        completed = row.get("completed")
        if not isinstance(task, str) or started is None or completed is None:
            continue
        try:
            duration = float(completed) - float(started)
        except TypeError, ValueError:
            continue
        if duration < 0:
            continue

        recipe_name = recipe if isinstance(recipe, str) else ""
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
    if dependency_source is not None:
        critical_path = _compute_critical_path(dependency_source, _duration_totals(durations))

    buildstats_join = BuildstatsJoin()
    cpu_floor = CpuFloor()
    task_churn = TaskChurn()
    if buildstats_source is not None:
        # Two sections read the same tree. Caching the zero-argument call keeps
        # the directory walk to one pass without either section having to know
        # the other exists; a raising source is not cached, so both still see
        # the failure and both still degrade on it.
        cached_source = functools.cache(buildstats_source)
        buildstats_join = _compute_join(cached_source, durations)
        cpu_floor = _compute_cpu_floor(buildstats_join, artifact)
        task_churn = _compute_churn(cached_source, durations)

    return TimingReport(
        top_slowest=top_slowest,
        critical_path=critical_path,
        regime=_regime_from(artifact, len(durations)),
        buildstats_join=buildstats_join,
        cpu_floor=cpu_floor,
        concurrency_floor=_compute_concurrency_floor(cpu_floor, critical_path, artifact),
        task_churn=task_churn,
    )
