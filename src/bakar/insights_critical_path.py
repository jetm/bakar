"""The critical-path sub-section: the longest dependency-respecting chain.

Split out of :mod:`bakar.insights_timing`. This is the opt-in section that
requires a ``dependency_source`` callable returning the ``(dot_text,
buildlist_text)`` pair from the run's own already-captured
``task-depends.dot`` (see ``commands.insights._dependency_source``) rather
than invoking a fresh ``bitbake -g <recipe>`` - the graph capture happens once,
at build time, and this module only ever reads it back.

Each chain node is weighted by the elapsed time of the executed task that
resolves to it (see :func:`bakar.insights_joins._resolve_graph_node`), never by
a recipe's summed task seconds. When ``dependency_source`` is omitted, or it
raises, or the resulting graph is empty/cyclic, or the graph-join rate falls
below its gate (:mod:`bakar.insights_joins`), :class:`CriticalPath` reports
``available=False`` with an explanatory ``note`` - the duration and top-N
sections of :mod:`bakar.insights_timing` never depend on this section's
success.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import networkx as nx

from bakar import graph_analyze
from bakar.insights_joins import _resolve_graph_node

if TYPE_CHECKING:
    from bakar.insights_joins import GraphJoin, _ParsedGraph
    from bakar.insights_timing import TaskDuration

#: How many of the critical path's heaviest nodes render in the report. A
#: task-level chain has no natural ceiling the way the retired recipe-level
#: chain did (274 possible nodes) - a real capture runs into the thousands
#: (design D6) - so the bound is a named constant rather than a number left
#: to the reader.
CRITICAL_PATH_TOP_N = 10


@dataclass(frozen=True)
class CriticalPath:
    """The critical-path sub-section: the longest dependency-respecting chain.

    ``available`` is ``False`` (the default) when no dependency source was
    supplied to :func:`bakar.insights_timing.timing_report`, when the supplied
    source failed, returned an empty graph, or returned a cyclic graph, or
    when the graph-join rate (see :class:`bakar.insights_joins.GraphJoin`)
    fell below its gate - in every one of those cases ``note`` explains why,
    and ``chain``/``total_seconds`` stay at their empty defaults. The join
    refusal is the one most likely to fire on a real, mostly-healthy run; the
    other four are rarer. The duration and top-N sections of
    :class:`bakar.insights_timing.TimingReport` never depend on this section's
    state.

    ``contributor`` maps a chain node to the name of the executed task that
    supplied its weight, and only carries an entry where that differs from
    the node's own bare name - the setscene-restore case, where a node's
    seconds came from the restore that stood in for it rather than from the
    node's own task. An ordinary node needs no entry: its contributor is
    itself.
    """

    available: bool = False
    chain: list[str] = field(default_factory=list)
    total_seconds: float = 0.0
    note: str = "critical-path unavailable"
    contributor: dict[str, str] = field(default_factory=dict)
    node_weights: dict[str, float] = field(default_factory=dict)

    def report_lines(self) -> list[str]:
        """Render this section as plain text lines.

        An unavailable path renders its note alone - no total, no node count,
        matching :meth:`bakar.insights_joins.BuildstatsJoin.report_lines`'s "no
        number without its caveat" rule. The available case ranks the chain by
        each node's OWN weight rather than chain order, since the point of the
        section is which link to shorten and that is the heaviest link
        regardless of where it sits on the chain, then bounds the rendered set
        at :data:`CRITICAL_PATH_TOP_N` - a task-level chain has no natural
        ceiling. A node whose weight came from a setscene restore names that
        restore, so the line never credits a bare node with seconds it never
        spent.

        A zero-weight node - the graph models work the build could do, and
        some chain nodes may not have executed at all - is never rendered:
        printing "0.0s" for a task that did not run is indistinguishable from
        one that ran in under 50ms, and such a node is by definition never
        "the link to shorten". The header names the chain's full node count
        (which can exceed the number of lines below it) as "nodes", not
        "tasks", since not every node on it necessarily ran.
        """
        if not self.available:
            return [f"  {self.note}"]

        lines = [f"  critical path: {self.total_seconds:.1f}s over {len(self.chain)} nodes"]
        weighted = [node for node in self.chain if self.node_weights.get(node, 0.0) > 0.0]
        ranked = sorted(weighted, key=lambda node: self.node_weights[node], reverse=True)
        for node in ranked[:CRITICAL_PATH_TOP_N]:
            weight = self.node_weights[node]
            contributor = self.contributor.get(node)
            via = f" (via {contributor})" if contributor and contributor != node else ""
            lines.append(f"  {node}: {weight:.1f}s{via}")
        return lines


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


def _compute_critical_path(
    parsed: _ParsedGraph,
    durations: list[TaskDuration],
    graph_join: GraphJoin,
) -> CriticalPath:
    """Compute the duration-weighted critical path over the TASK-level graph.

    ``graph_join`` owns the gate decision (see
    :class:`bakar.insights_joins.GraphJoin`); this function reads it and
    refuses with the join's own wording rather than re-deriving a second
    verdict from the same numbers. Any other failure - the source raised, the
    graph is empty, or it is cyclic - degrades to an explicit "unavailable"
    :class:`CriticalPath` with a note; this function never raises back to
    :func:`bakar.insights_timing.timing_report`.

    Node weights come from ``durations``, NOT from the join's ``executed``
    denominator - ``durations`` is built after the timestamp guards in
    :func:`bakar.insights_timing.timing_report` and carries real elapsed
    seconds, while ``executed`` is identity-only and built before them (see
    the comment there). Weighting from ``executed`` would reintroduce the
    vacuous-100% failure that split the two sets in the first place. A node no
    duration resolves to is simply absent from ``node_weights``;
    :func:`_weighted_longest_path` treats a missing key as weight ``0.0`` and
    must not be made unavailable by it.
    """
    if parsed.error is not None:
        return CriticalPath(note=f"critical-path unavailable: {parsed.error}")
    if parsed.graph is None or parsed.graph.number_of_nodes() == 0:
        return CriticalPath(note="critical-path unavailable: empty dependency graph")
    if not graph_join.gate_passed:
        return CriticalPath(note=f"critical-path unavailable: {graph_join.note}")

    task_graph = graph_analyze.to_task_digraph(parsed.graph)
    if not nx.is_directed_acyclic_graph(task_graph):
        # find_cycle names the offending nodes so a refusal has a locus, the
        # same way the graph-join refusal names its unjoined sample rather
        # than only a count.
        cycle = graph_analyze.find_cycle(task_graph)
        locus = f": {' -> '.join(cycle)}" if cycle else ""
        return CriticalPath(note=f"critical-path unavailable: cyclic task dependency graph{locus}")

    node_resolutions: dict[str, list[tuple[str, float]]] = {}
    for d in durations:
        resolved = _resolve_graph_node(parsed.nodes, d.recipe, d.task)
        if resolved is None:
            continue
        node, contributing_task = resolved
        node_resolutions.setdefault(node, []).append((contributing_task, d.duration))

    # A node is weighted by exactly ONE executed task's own duration, never a
    # sum: a failed setscene restore followed by the real task (or the reverse
    # order) resolves both rows to the same node, and the restore's seconds are
    # not part of the serial cost the real execution represents at that graph
    # position. The real (non-restore) execution always wins over a restore
    # regardless of which duration is larger; a tie between two candidates of
    # the same kind keeps the larger one.
    node_weights: dict[str, float] = {}
    contributor: dict[str, str] = {}
    for node, resolutions in node_resolutions.items():
        node_task = node.rsplit(".", 1)[-1]
        contributing_task, weight = max(resolutions, key=lambda pair: (pair[0] == node_task, pair[1]))
        node_weights[node] = weight
        if contributing_task != node_task:
            contributor[node] = contributing_task

    chain, total = _weighted_longest_path(task_graph, node_weights)
    chain_contributor = {node: contributor[node] for node in chain if node in contributor}
    chain_weights = {node: node_weights[node] for node in chain if node in node_weights}
    if not chain_weights:
        # The graph join passed (every executed task resolved to SOME node),
        # but none of them landed on this chain with a usable duration - an
        # artifact whose rows carry no started/completed pair. Publishing
        # total_seconds=0.0 as available=True would let a CPU-only floor
        # render under the concurrency-floor label, which is the exact
        # failure the capability docs promise cannot happen.
        return CriticalPath(note="critical-path unavailable: no executed task's duration resolved to any graph node")
    return CriticalPath(
        available=True,
        chain=chain,
        total_seconds=total,
        note="critical-path computed",
        contributor=chain_contributor,
        node_weights=chain_weights,
    )
