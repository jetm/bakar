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
class TimingReport:
    """The timing report: top-N slowest tasks plus the critical-path section."""

    top_slowest: list[TaskDuration] = field(default_factory=list)
    critical_path: CriticalPath = field(default_factory=CriticalPath)


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

    for _u, v, data in pn_graph.edges(data=True):
        data["weight"] = duration_totals.get(v, 0.0)

    chain = list(nx.dag_longest_path(pn_graph, weight="weight"))
    total = sum(duration_totals.get(name, 0.0) for name in chain)
    return CriticalPath(available=True, chain=chain, total_seconds=total, note="critical-path computed")


def timing_report(
    artifact: dict | list,
    top_n: int = DEFAULT_TOP_N,
    *,
    baselines_path: Path | None = None,
    dependency_source: Callable[[], tuple[str, str]] | None = None,
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

    return TimingReport(top_slowest=top_slowest, critical_path=critical_path)
