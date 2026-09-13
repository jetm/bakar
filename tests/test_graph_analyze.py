"""Unit tests for :mod:`bakar.graph_analyze`.

All tests are pure: no subprocess, no container, no filesystem I/O beyond
reading the fixture file at module load time.  The fixture
``tests/fixtures/task-depends.dot`` captures a small but realistic
``bitbake -g`` task-dependency graph; the cyclic case is an inline string so
the file fixture stays acyclic for the DAG-based assertions.
"""

from __future__ import annotations

from pathlib import Path

import networkx as nx
import pytest

from bakar.graph_analyze import (
    _is_log_line,
    _strip_kas_preamble,
    analyze,
    blast_radius,
    collapse_to_pn,
    critical_nodes,
    find_cycle,
    longest_chain,
    package_count,
    read_graph,
    to_task_digraph,
    top_runtime_packages,
)
from tests.conftest import PN_CYCLE_TASK_DAG_DOT

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parent / "fixtures"

# A seeded three-recipe cycle a -> b -> c -> a at the task level.
CYCLE_DOT = (
    "digraph depends {\n"
    '"a.do_compile" -> "b.do_compile"\n'
    '"b.do_compile" -> "c.do_compile"\n'
    '"c.do_compile" -> "a.do_compile"\n'
    "}\n"
)

# PN_CYCLE_TASK_DAG_DOT (a task-level DAG whose PN collapse is cyclic, shared
# with test_cli_graph.py via conftest.py so the shape cannot drift between
# them) is imported above. CYCLE_DOT cannot stand in for it - that one is
# cyclic at both levels, so it proves nothing about the collapse.

# A small buildhistory-style runtime graph: libc is depended on by two pkgs.
RUNTIME_DOT = 'digraph depends {\n"busybox" -> "glibc"\n"bash" -> "glibc"\n"bash" -> "ncurses"\n}\n'


@pytest.fixture(scope="module")
def dot_text() -> str:
    return (FIXTURES / "task-depends.dot").read_text()


@pytest.fixture(scope="module")
def buildlist_text() -> str:
    return "busybox\nglibc\ngcc-cross\nbinutils-cross\n"


# ===========================================================================
# read_graph
# ===========================================================================


class TestStripKasPreamble:
    def test_strips_log_lines_before_digraph(self) -> None:
        """kas-container startup log noise before the DOT block is removed."""
        noisy = "2026-06-03 15:03:32 - INFO - kas 5.2 started on Fedora Linux 40\ndigraph d {\n}\n"
        assert _strip_kas_preamble(noisy).startswith("digraph")

    def test_clean_dot_unchanged(self) -> None:
        clean = "digraph d {\n}\n"
        assert _strip_kas_preamble(clean) == clean

    def test_no_dot_keyword_returns_original(self) -> None:
        # If there's no digraph/graph, return unchanged (caller will get empty graph).
        text = "only log noise here\n"
        assert _strip_kas_preamble(text) == text


class TestIsLogLine:
    def test_timestamp_line_is_log(self) -> None:
        assert _is_log_line("2026-06-03 15:03:32 - INFO - kas 5.2 started")

    def test_recipe_name_is_not_log(self) -> None:
        assert not _is_log_line("busybox")

    def test_empty_is_not_log(self) -> None:
        assert not _is_log_line("")


def _graph(dot_text: str) -> nx.MultiDiGraph:
    """``read_graph``'s graph half, for tests whose subject is not the flag."""
    graph, _parsed_ok = read_graph(dot_text)
    return graph


class TestReadGraph:
    def test_fixture_parses_non_empty(self, dot_text: str) -> None:
        """The falsifier: the parse must turn the fixture into a non-empty graph."""
        graph, parsed_ok = read_graph(dot_text)
        assert parsed_ok
        assert graph.number_of_nodes() > 0
        assert graph.is_directed()

    def test_kas_preamble_stripped_before_parse(self) -> None:
        """Log noise prepended by run_shell_capture does not prevent parsing."""
        noisy_dot = (
            "2026-06-03 15:03:32 - INFO     - kas 5.2 started on Fedora Linux 40\n"
            'digraph depends {\n"a.do_compile" -> "b.do_compile"\n}\n'
        )
        graph, parsed_ok = read_graph(noisy_dot)
        assert parsed_ok
        assert graph.number_of_nodes() > 0

    def test_empty_text_reads_as_empty_not_unparseable(self) -> None:
        """An absent capture is empty, and says so: nothing failed to parse."""
        graph, parsed_ok = read_graph("")
        assert parsed_ok
        assert graph.number_of_nodes() == 0

    def test_whitespace_only_reads_as_empty_not_unparseable(self) -> None:
        graph, parsed_ok = read_graph("   \n\t\n")
        assert parsed_ok
        assert graph.number_of_nodes() == 0

    def test_malformed_is_flagged_unparseable_not_empty(self) -> None:
        """The falsifier for the flag: both cases return an empty graph, so only
        ``parsed_ok`` tells a malformed artifact apart from an absent one."""
        graph, parsed_ok = read_graph("this is not dot {{{ -> -> ->")
        assert not parsed_ok
        assert isinstance(graph, nx.MultiDiGraph)
        assert graph.number_of_nodes() == 0

    def test_unparseable_input_pydot_rejects_without_raising(self) -> None:
        """``graph_from_dot_data`` returns None rather than raising on this, so
        the exception guard alone would let it through as a clean parse."""
        graph, parsed_ok = read_graph("digraph { this is ]] not valid")
        assert not parsed_ok
        assert graph.number_of_nodes() == 0

    def test_malformed_input_never_leaks_pydot_diagnostics_to_stdout(self, capsys) -> None:
        """pydot prints its own caret diagnostic straight to stdout on rejected
        input rather than raising or writing to stderr - the one part of this
        call this module does not otherwise control. A command downstream of a
        malformed captured graph (bakar graph --json, bakar insights) treats
        stdout as a machine-readable payload, so parser prose landing there
        would corrupt it."""
        read_graph("this is not dot {{{ -> -> ->")
        captured = capsys.readouterr()
        assert captured.out == ""


# ===========================================================================
# collapse_to_pn
# ===========================================================================


class TestCollapseToPn:
    def test_task_suffix_stripped(self, dot_text: str) -> None:
        pn = collapse_to_pn(_graph(dot_text))
        assert set(pn.nodes) == {"busybox", "glibc", "gcc-cross", "binutils-cross"}

    def test_self_loops_dropped(self) -> None:
        """An edge between two tasks of the same recipe is not a PN self-loop."""
        text = '"busybox.do_install" -> "busybox.do_compile"\n'
        pn = collapse_to_pn(_graph("digraph d {\n" + text + "}\n"))
        assert not any(s == d for s, d in pn.edges())

    def test_parallel_edges_merged(self) -> None:
        """Two task edges collapsing to the same PN pair yield one edge."""
        text = '"a.do_compile" -> "b.do_compile"\n"a.do_install" -> "b.do_populate_sysroot"\n'
        pn = collapse_to_pn(_graph("digraph d {\n" + text + "}\n"))
        assert pn.number_of_edges() == 1


# ===========================================================================
# to_task_digraph
# ===========================================================================


class TestToTaskDigraph:
    def test_node_names_kept_verbatim(self, dot_text: str) -> None:
        """Nodes stay ``<pn>.<task>``; nothing is collapsed to a recipe name."""
        task_graph = to_task_digraph(_graph(dot_text))
        assert "busybox.do_compile" in task_graph.nodes
        assert "busybox" not in task_graph.nodes

    def test_every_node_survives(self, dot_text: str) -> None:
        multi = _graph(dot_text)
        assert set(to_task_digraph(multi).nodes) == set(multi.nodes)

    def test_parallel_edges_merged(self) -> None:
        """A DiGraph cannot hold multiplicity; the edge itself is kept."""
        text = '"a.do_compile" -> "b.do_compile"\n"a.do_compile" -> "b.do_compile"\n'
        task_graph = to_task_digraph(_graph("digraph d {\n" + text + "}\n"))
        assert task_graph.number_of_edges() == 1
        assert task_graph.has_edge("a.do_compile", "b.do_compile")

    def test_task_level_dag_survives_a_cyclic_pn_collapse(self) -> None:
        """The falsifier: the collapse is what makes a real OE graph cyclic.

        Asserting both directions on the same input is the point - a fixture
        whose PN collapse were also acyclic would let this pass for the wrong
        reason.
        """
        multi = _graph(PN_CYCLE_TASK_DAG_DOT)
        assert nx.is_directed_acyclic_graph(to_task_digraph(multi))
        assert not nx.is_directed_acyclic_graph(collapse_to_pn(multi))

    def test_task_level_cycle_is_not_hidden(self) -> None:
        """A genuine task-level cycle stays visible rather than being dropped."""
        assert not nx.is_directed_acyclic_graph(to_task_digraph(_graph(CYCLE_DOT)))

    def test_empty_graph_returns_empty(self) -> None:
        assert to_task_digraph(nx.MultiDiGraph()).number_of_nodes() == 0


# ===========================================================================
# package_count
# ===========================================================================


class TestPackageCount:
    def test_matches_line_count(self, buildlist_text: str) -> None:
        """The falsifier: package count matches the pn-buildlist line count."""
        assert package_count(buildlist_text) == 4

    def test_blank_lines_ignored(self) -> None:
        assert package_count("busybox\n\n  \nglibc\n") == 2

    def test_empty_returns_zero(self) -> None:
        assert package_count("") == 0

    def test_log_lines_excluded_from_count(self) -> None:
        """kas startup log lines prepended to pn-buildlist are not counted as recipes."""
        noisy = "2026-06-03 15:03:32 - INFO - kas 5.2 started\nbusybox\nglibc\n"
        assert package_count(noisy) == 2


# ===========================================================================
# blast_radius / depth bounding
# ===========================================================================


class TestBlastRadius:
    def test_full_transitive_closure(self, dot_text: str) -> None:
        pn = collapse_to_pn(_graph(dot_text))
        assert blast_radius(pn, "busybox") == 3

    def test_depth_one_caps_expansion(self, dot_text: str) -> None:
        pn = collapse_to_pn(_graph(dot_text))
        assert blast_radius(pn, "busybox", depth=1) == 2

    def test_depth_two_caps_expansion(self, dot_text: str) -> None:
        """The falsifier: --depth must not return nodes deeper than N levels."""
        pn = collapse_to_pn(_graph(dot_text))
        assert blast_radius(pn, "busybox", depth=2) == 3

    def test_depth_bound_below_full(self, dot_text: str) -> None:
        pn = collapse_to_pn(_graph(dot_text))
        full = blast_radius(pn, "busybox")
        bounded = blast_radius(pn, "busybox", depth=1)
        assert bounded < full

    def test_missing_target_returns_zero(self, dot_text: str) -> None:
        pn = collapse_to_pn(_graph(dot_text))
        assert blast_radius(pn, "nonexistent") == 0


# ===========================================================================
# longest_chain
# ===========================================================================


class TestLongestChain:
    def test_returns_path(self, dot_text: str) -> None:
        pn = collapse_to_pn(_graph(dot_text))
        chain = longest_chain(pn)
        assert chain[0] == "busybox"
        assert chain[-1] == "binutils-cross"

    def test_cyclic_returns_empty(self) -> None:
        pn = collapse_to_pn(_graph(CYCLE_DOT))
        assert longest_chain(pn) == []

    def test_task_level_dag_with_cyclic_pn_collapse_is_non_empty(self) -> None:
        """The falsifier: the PN collapse being cyclic must not empty this out."""
        task_graph = to_task_digraph(_graph(PN_CYCLE_TASK_DAG_DOT))
        assert longest_chain(task_graph) != []

    def test_empty_returns_empty(self) -> None:
        assert longest_chain(nx.DiGraph()) == []


# ===========================================================================
# find_cycle
# ===========================================================================


class TestFindCycle:
    def test_acyclic_reports_none(self, dot_text: str) -> None:
        """The falsifier: cycle detection reports none for an acyclic graph."""
        pn = collapse_to_pn(_graph(dot_text))
        assert find_cycle(pn) == []

    def test_seeded_cycle_found(self) -> None:
        """The falsifier: cycle detection finds the seeded cycle."""
        pn = collapse_to_pn(_graph(CYCLE_DOT))
        names = find_cycle(pn)
        assert set(names) == {"a", "b", "c"}

    def test_task_level_dag_with_cyclic_pn_collapse_reports_none(self) -> None:
        """The falsifier: the PN collapse being cyclic must not report a cycle."""
        task_graph = to_task_digraph(_graph(PN_CYCLE_TASK_DAG_DOT))
        assert find_cycle(task_graph) == []

    def test_empty_graph_reports_none(self) -> None:
        assert find_cycle(nx.DiGraph()) == []


# ===========================================================================
# critical_nodes
# ===========================================================================


class TestCriticalNodes:
    def test_highest_in_degree_first(self, dot_text: str) -> None:
        pn = collapse_to_pn(_graph(dot_text))
        ranked = critical_nodes(pn)
        assert ranked[0] == ("gcc-cross", 2)

    def test_zero_in_degree_omitted(self, dot_text: str) -> None:
        pn = collapse_to_pn(_graph(dot_text))
        names = [n for n, _ in critical_nodes(pn)]
        assert "busybox" not in names

    def test_top_n_limit(self, dot_text: str) -> None:
        pn = collapse_to_pn(_graph(dot_text))
        assert len(critical_nodes(pn, top_n=1)) == 1


# ===========================================================================
# top_runtime_packages
# ===========================================================================


class TestTopRuntimePackages:
    def test_fan_in_ranking(self) -> None:
        ranked = top_runtime_packages(RUNTIME_DOT)
        assert ranked[0] == ("glibc", 2)

    def test_empty_returns_empty(self) -> None:
        assert top_runtime_packages("") == []


# ===========================================================================
# analyze (aggregate)
# ===========================================================================


class TestAnalyze:
    def test_assembles_all_keys(self, dot_text: str, buildlist_text: str) -> None:
        result = analyze(dot_text, buildlist_text, "busybox")
        assert result["package_count"] == 4
        assert result["blast_radius"] == 3
        assert result["cycle"] == []
        assert result["longest_chain"][0].startswith("busybox.do_")
        assert "direct_deps" in result
        assert isinstance(result["direct_deps"], list)

    def test_task_level_dag_with_cyclic_pn_collapse(self) -> None:
        """The falsifier: a PN-cyclic/task-acyclic graph must not empty the
        chain or fake a cycle, while PN-level insights stay unaffected."""
        result = analyze(PN_CYCLE_TASK_DAG_DOT, "a\nb\n", "a")
        assert result["cycle"] == []
        assert result["longest_chain"] != []
        assert all(".do_" in node for node in result["longest_chain"])
        # PN-level insights still read the PN-collapsed graph, bare recipe names.
        assert result["direct_deps"] == ["b"]
        assert result["blast_radius"] == 1
        assert result["critical"] == [("a", 1), ("b", 1)]

    def test_genuine_task_level_cycle_reports_task_nodes(self) -> None:
        """The falsifier: a real cycle must name task nodes, not bare recipes."""
        result = analyze(CYCLE_DOT, "a\nb\nc\n", "a")
        assert result["cycle"] != []
        assert all(node.startswith(tuple("abc")) and ".do_" in node for node in result["cycle"])

    def test_depth_propagates(self, dot_text: str, buildlist_text: str) -> None:
        result = analyze(dot_text, buildlist_text, "busybox", depth=1)
        assert result["blast_radius"] == 2
        assert result["depth"] == 1

    def test_empty_dot_does_not_crash(self, buildlist_text: str) -> None:
        result = analyze("", buildlist_text, "busybox")
        assert result["blast_radius"] == 0
        assert result["cycle"] == []
