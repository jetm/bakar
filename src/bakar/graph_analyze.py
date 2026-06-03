"""Pure analysis of ``bitbake -g`` dependency-graph artifacts.

No Typer, no subprocess, no container exec.  Every function takes text as
input (the contents of ``task-depends.dot``, ``pn-buildlist``, or a
buildhistory ``depends.dot``) and returns plain Python values.  The command
module (``commands/graph.py``) handles container retrieval and printing.

The ``task-depends.dot`` graph from ``bitbake -g`` has one node per *task*
(``"busybox.do_compile"``).  An edge ``A -> B`` means task ``A`` depends on
task ``B``.  For human-facing analysis we collapse tasks to their package
name (PN) by stripping the ``.do_*`` suffix and merging parallel edges, then
run the graph algorithms on that PN-level :class:`networkx.DiGraph`.

Functions
---------
read_graph(dot_text)
    Parse ``task-depends.dot`` text into a task-level MultiDiGraph.
collapse_to_pn(graph)
    Collapse a task-level graph to a PN-level DiGraph (suffix stripped,
    self-loops and parallel edges dropped).
package_count(buildlist_text)
    Count non-empty lines in ``pn-buildlist``.
blast_radius(pn_graph, target, depth=None)
    Count of transitive dependencies of *target* (optionally depth-bounded).
longest_chain(pn_graph)
    The longest dependency path through the DAG (empty if cyclic).
find_cycle(pn_graph)
    The first cycle as a list of PN names, or ``[]`` when acyclic.
critical_nodes(pn_graph, top_n=5)
    PNs with the highest in-degree (most depended-on).
top_runtime_packages(depends_dot_text, top_n=5)
    Top runtime packages by fan-in from a buildhistory ``depends.dot``.
direct_deps(pn_graph, target)
    Sorted list of packages *target* directly depends on.
analyze(dot_text, buildlist_text, target, depth=None)
    Assemble every insight into one dict for the command's JSON/text output.
"""

from __future__ import annotations

import os
import re
import tempfile

import networkx as nx
from networkx.drawing.nx_pydot import read_dot


def _strip_kas_preamble(text: str) -> str:
    """Strip kas-container startup log lines that precede the DOT graph content.

    ``run_shell_capture`` merges stderr into the captured file, so kas startup
    messages like ``2026-06-03 15:03:32 - INFO - kas 5.2 started ...`` appear
    before the ``digraph { ... }`` block.  pydot's parser fails on the first
    non-DOT token; this function finds the first ``digraph``/``graph``/``strict``
    keyword and returns the text from that point onward.
    """
    m = re.search(r"(?im)^(strict\s+)?(di)?graph\b", text)
    return text[m.start() :] if m else text


def _is_log_line(line: str) -> bool:
    """Return True for kas-container log lines that appear in captured output.

    Log lines start with a timestamp digit (``2026-...``) or a known log-level
    keyword.  Yocto recipe names never contain spaces or start with a digit
    (with the rare exception of packages like ``389-ds-base`` which start with
    a digit but have no spaces); the space-based check safely distinguishes them.
    """
    stripped = line.strip()
    if not stripped:
        return False
    # Log lines always contain at least one space (e.g. "INFO - kas 5.2 started")
    # while recipe names never contain spaces.
    return " " in stripped


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------


def read_graph(dot_text: str) -> nx.MultiDiGraph:
    """Parse ``task-depends.dot`` text into a task-level MultiDiGraph.

    ``networkx.drawing.nx_pydot.read_dot`` only reads from a path, so the
    text is written to a temp file first and removed afterward.  Returns an
    empty graph - not raising - on empty or unparseable input, so a malformed
    artifact does not crash the whole command.
    """
    if not dot_text or not dot_text.strip():
        return nx.MultiDiGraph()

    # Strip kas-container startup log noise that precedes the DOT content.
    dot_text = _strip_kas_preamble(dot_text)

    path = None
    try:
        fd, path = tempfile.mkstemp(suffix=".dot")
        with os.fdopen(fd, "w") as f:
            f.write(dot_text)
        graph = read_dot(path)
    except Exception:
        return nx.MultiDiGraph()
    finally:
        if path is not None and os.path.exists(path):
            os.unlink(path)

    # read_dot returns a MultiGraph for an undirected source; bitbake always
    # emits a digraph, but normalize defensively.
    if not graph.is_directed():
        graph = nx.MultiDiGraph(graph)
    return graph


def _strip_task(node: str) -> str:
    """Strip the ``.do_*`` task suffix from a task node name.

    ``"busybox.do_compile"`` -> ``"busybox"``.  A node without a ``.do_``
    segment is returned unchanged.
    """
    idx = node.rfind(".do_")
    if idx == -1:
        return node
    return node[:idx]


def collapse_to_pn(graph: nx.MultiDiGraph) -> nx.DiGraph:
    """Collapse a task-level graph to a PN-level :class:`networkx.DiGraph`.

    Each task node is mapped to its PN (``.do_*`` suffix stripped).  Parallel
    edges are merged and self-loops (a PN depending on its own other tasks)
    are dropped, so the result is a simple directed graph suitable for
    ``descendants``/``dag_longest_path``/``find_cycle``.
    """
    pn_graph: nx.DiGraph = nx.DiGraph()
    for node in graph.nodes:
        pn_graph.add_node(_strip_task(node))
    for src, dst in graph.edges():
        psrc, pdst = _strip_task(src), _strip_task(dst)
        if psrc != pdst:
            pn_graph.add_edge(psrc, pdst)
    return pn_graph


# ---------------------------------------------------------------------------
# pn-buildlist
# ---------------------------------------------------------------------------


def package_count(buildlist_text: str) -> int:
    """Count the recipes in ``pn-buildlist``, skipping kas-container log lines.

    Recipe names never contain spaces; log lines (timestamps, INFO/WARNING
    notices) always do, so the space check reliably separates them.
    """
    if not buildlist_text:
        return 0
    return sum(1 for line in buildlist_text.splitlines() if line.strip() and not _is_log_line(line))


# ---------------------------------------------------------------------------
# Graph insights
# ---------------------------------------------------------------------------


def blast_radius(pn_graph: nx.DiGraph, target: str, depth: int | None = None) -> int:
    """Count *target*'s transitive dependencies (its descendants).

    With *depth* set to a positive int, expansion is bounded to that many
    levels via a breadth-first walk; ``None`` (the default) counts the full
    transitive closure.  Returns 0 when *target* is absent from the graph.
    """
    if target not in pn_graph:
        return 0
    if depth is None:
        return len(nx.descendants(pn_graph, target))
    return len(_bounded_descendants(pn_graph, target, depth))


def _bounded_descendants(pn_graph: nx.DiGraph, target: str, depth: int) -> set[str]:
    """Descendants of *target* reachable within *depth* edges (BFS)."""
    if depth <= 0:
        return set()
    seen: set[str] = set()
    frontier = {target}
    for _ in range(depth):
        nxt: set[str] = set()
        for node in frontier:
            for succ in pn_graph.successors(node):
                if succ not in seen and succ != target:
                    seen.add(succ)
                    nxt.add(succ)
        frontier = nxt
        if not frontier:
            break
    return seen


def longest_chain(pn_graph: nx.DiGraph) -> list[str]:
    """Return the longest dependency path through the DAG.

    Uses :func:`networkx.dag_longest_path`, which requires a DAG; returns an
    empty list when the graph is cyclic or empty rather than raising.
    """
    if pn_graph.number_of_nodes() == 0:
        return []
    if not nx.is_directed_acyclic_graph(pn_graph):
        return []
    return list(nx.dag_longest_path(pn_graph))


def find_cycle(pn_graph: nx.DiGraph) -> list[str]:
    """Return the recipes in the first detected cycle, or ``[]`` if acyclic.

    Wraps :func:`networkx.find_cycle`, which raises
    :class:`networkx.NetworkXNoCycle` on an acyclic graph; that case maps to
    an empty list.  The returned list is the ordered PN names participating in
    the cycle.
    """
    try:
        edges = nx.find_cycle(pn_graph, orientation="original")
    except nx.NetworkXNoCycle:
        return []
    except nx.NetworkXError:
        return []
    names: list[str] = []
    for edge in edges:
        src = edge[0]
        if src not in names:
            names.append(src)
    return names


def critical_nodes(pn_graph: nx.DiGraph, top_n: int = 5) -> list[tuple[str, int]]:
    """Return the *top_n* PNs with the highest in-degree (most depended-on).

    Each entry is ``(pn, in_degree)``; ties are broken by name for a stable
    order.  Nodes with zero in-degree are omitted.
    """
    ranked = sorted(
        ((node, deg) for node, deg in pn_graph.in_degree() if deg > 0),
        key=lambda item: (-item[1], item[0]),
    )
    return ranked[:top_n]


# ---------------------------------------------------------------------------
# Buildhistory depends.dot (optional, runtime-dependency graph)
# ---------------------------------------------------------------------------


def top_runtime_packages(depends_dot_text: str, top_n: int = 5) -> list[tuple[str, int]]:
    """Top runtime packages by fan-in from a buildhistory ``depends.dot``.

    Buildhistory's ``depends.dot`` is a package-level runtime-dependency
    graph; the most-depended-on packages have the highest in-degree.  Returns
    an empty list - not raising - on empty or unparseable input.
    """
    graph = read_graph(depends_dot_text)
    if graph.number_of_nodes() == 0:
        return []
    simple: nx.DiGraph = nx.DiGraph()
    for node in graph.nodes:
        simple.add_node(node)
    for src, dst in graph.edges():
        if src != dst:
            simple.add_edge(src, dst)
    return critical_nodes(simple, top_n)


# ---------------------------------------------------------------------------
# Aggregate
# ---------------------------------------------------------------------------


def analyze(
    dot_text: str,
    buildlist_text: str,
    target: str,
    depth: int | None = None,
) -> dict[str, object]:
    """Assemble every insight into one dict for JSON/text rendering.

    Container-free: callers pass the already-retrieved artifact text.  The
    returned dict carries ``package_count``, ``blast_radius``, ``longest_chain``,
    ``cycle``, and ``critical`` keys.  A malformed or empty dot yields an empty
    PN graph, so the numeric insights degrade to 0/empty rather than crashing.
    """
    pn_graph = collapse_to_pn(read_graph(dot_text))
    direct = sorted(pn_graph.successors(target)) if target in pn_graph else []
    return {
        "target": target,
        "depth": depth,
        "package_count": package_count(buildlist_text),
        "direct_deps": direct,
        "blast_radius": blast_radius(pn_graph, target, depth),
        "longest_chain": longest_chain(pn_graph),
        "cycle": find_cycle(pn_graph),
        "critical": critical_nodes(pn_graph),
    }
