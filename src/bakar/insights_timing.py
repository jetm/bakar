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

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import networkx as nx

from bakar import graph_analyze, task_timings
from bakar.task_rollup import _tasks_from

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from bakar.buildstats import BuildstatsRun

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
class TimingReport:
    """The timing report: top-N slowest tasks plus the critical-path section."""

    top_slowest: list[TaskDuration] = field(default_factory=list)
    critical_path: CriticalPath = field(default_factory=CriticalPath)
    regime: Regime = field(default_factory=Regime)
    buildstats_join: BuildstatsJoin = field(default_factory=BuildstatsJoin)


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
    """Key both sides of the join on ``(PN, task)``.

    The event log records a versioned PF (``busybox-1.36.1-r0``) and so does the
    buildstats recipe directory, but they are not guaranteed to agree on the
    revision suffix - a task restored from sstate and one rebuilt after a bump
    can disagree by ``-r0`` alone. Stripping the version the way
    :func:`bakar.task_timings.strip_recipe_version` already does for baseline
    keys puts both sides in one namespace, so a shortfall in the rate means a
    genuinely missing record rather than a spelling difference.
    """
    return (task_timings.strip_recipe_version(recipe), task)


def _compute_join(
    buildstats_source: Callable[[], BuildstatsRun],
    durations: list[TaskDuration],
) -> BuildstatsJoin:
    """Join executed tasks against buildstats records and gate on the rate.

    Follows :func:`_compute_critical_path`'s precedent exactly: any failure -
    the callable raises, the tree is absent, the tree is empty - returns an
    explicit unavailable result with a note and never raises back to
    :func:`timing_report`.

    ``absent`` and ``empty`` keep separate notes. They are the two outcomes
    :mod:`bakar.buildstats` went out of its way to distinguish, and collapsing
    them here would put the distinction back in the bin it was lifted out of:
    a tree that was never found is a path problem, a tree that recorded nothing
    is a measurement.
    """
    try:
        run = buildstats_source()
    except Exception as exc:  # noqa: BLE001 - any buildstats-source failure degrades gracefully
        return BuildstatsJoin(note=f"buildstats join unavailable: source failed ({exc})")

    if run.outcome == "absent":
        return BuildstatsJoin(note=f"buildstats join unavailable: tree absent ({run.note})")
    if run.outcome != "parsed":
        return BuildstatsJoin(
            note=f"buildstats join unavailable: tree present but recorded nothing ({run.note})",
        )

    records: dict[tuple[str, str], float] = {}
    for stat in run.tasks:
        key = _join_key(stat.recipe, stat.task)
        records[key] = records.get(key, 0.0) + stat.cpu_seconds

    matched: set[tuple[str, str]] = set()
    unjoined: list[str] = []
    joined = 0
    for d in durations:
        key = _join_key(d.recipe, d.task)
        if key in records:
            joined += 1
            matched.add(key)
        else:
            unjoined.append(f"{key[0]}:{key[1]}")

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
                f"buildstats record) - no CPU-derived figure is reported for this run"
            ),
        )

    # Only records an executed task actually matched contribute. A buildstats
    # tree can carry rows this run never executed (a stale capture, or a task
    # from a sibling machine's directory), and summing the tree wholesale would
    # credit them to this build.
    return BuildstatsJoin(
        available=True,
        gate_passed=True,
        executed=executed,
        joined=joined,
        cpu_seconds=sum(records[k] for k in matched),
        note=(f"buildstats join {rate_pct:.1f}% ({joined} of {executed} executed tasks matched a buildstats record)"),
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
    degrades the same way ``dependency_source`` does.
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
    if buildstats_source is not None:
        buildstats_join = _compute_join(buildstats_source, durations)

    return TimingReport(
        top_slowest=top_slowest,
        critical_path=critical_path,
        regime=_regime_from(artifact, len(durations)),
        buildstats_join=buildstats_join,
    )
