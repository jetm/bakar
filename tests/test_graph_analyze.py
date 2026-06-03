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
    top_runtime_packages,
)

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


class TestReadGraph:
    def test_fixture_parses_non_empty(self, dot_text: str) -> None:
        """The falsifier: read_dot must parse the fixture into a non-empty graph."""
        graph = read_graph(dot_text)
        assert graph.number_of_nodes() > 0
        assert graph.is_directed()

    def test_kas_preamble_stripped_before_parse(self) -> None:
        """Log noise prepended by run_shell_capture does not prevent parsing."""
        noisy_dot = (
            "2026-06-03 15:03:32 - INFO     - kas 5.2 started on Fedora Linux 40\n"
            'digraph depends {\n"a.do_compile" -> "b.do_compile"\n}\n'
        )
        graph = read_graph(noisy_dot)
        assert graph.number_of_nodes() > 0

    def test_empty_text_returns_empty_graph(self) -> None:
        graph = read_graph("")
        assert graph.number_of_nodes() == 0

    def test_whitespace_only_returns_empty_graph(self) -> None:
        assert read_graph("   \n\t\n").number_of_nodes() == 0

    def test_malformed_does_not_raise(self) -> None:
        """A malformed dot returns an empty graph rather than crashing."""
        graph = read_graph("this is not dot {{{ -> -> ->")
        assert isinstance(graph, nx.MultiDiGraph)


# ===========================================================================
# collapse_to_pn
# ===========================================================================


class TestCollapseToPn:
    def test_task_suffix_stripped(self, dot_text: str) -> None:
        pn = collapse_to_pn(read_graph(dot_text))
        assert set(pn.nodes) == {"busybox", "glibc", "gcc-cross", "binutils-cross"}

    def test_self_loops_dropped(self) -> None:
        """An edge between two tasks of the same recipe is not a PN self-loop."""
        text = '"busybox.do_install" -> "busybox.do_compile"\n'
        pn = collapse_to_pn(read_graph("digraph d {\n" + text + "}\n"))
        assert not any(s == d for s, d in pn.edges())

    def test_parallel_edges_merged(self) -> None:
        """Two task edges collapsing to the same PN pair yield one edge."""
        text = '"a.do_compile" -> "b.do_compile"\n"a.do_install" -> "b.do_populate_sysroot"\n'
        pn = collapse_to_pn(read_graph("digraph d {\n" + text + "}\n"))
        assert pn.number_of_edges() == 1


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
        pn = collapse_to_pn(read_graph(dot_text))
        assert blast_radius(pn, "busybox") == 3

    def test_depth_one_caps_expansion(self, dot_text: str) -> None:
        pn = collapse_to_pn(read_graph(dot_text))
        assert blast_radius(pn, "busybox", depth=1) == 2

    def test_depth_two_caps_expansion(self, dot_text: str) -> None:
        """The falsifier: --depth must not return nodes deeper than N levels."""
        pn = collapse_to_pn(read_graph(dot_text))
        assert blast_radius(pn, "busybox", depth=2) == 3

    def test_depth_bound_below_full(self, dot_text: str) -> None:
        pn = collapse_to_pn(read_graph(dot_text))
        full = blast_radius(pn, "busybox")
        bounded = blast_radius(pn, "busybox", depth=1)
        assert bounded < full

    def test_missing_target_returns_zero(self, dot_text: str) -> None:
        pn = collapse_to_pn(read_graph(dot_text))
        assert blast_radius(pn, "nonexistent") == 0


# ===========================================================================
# longest_chain
# ===========================================================================


class TestLongestChain:
    def test_returns_path(self, dot_text: str) -> None:
        pn = collapse_to_pn(read_graph(dot_text))
        chain = longest_chain(pn)
        assert chain[0] == "busybox"
        assert chain[-1] == "binutils-cross"

    def test_cyclic_returns_empty(self) -> None:
        pn = collapse_to_pn(read_graph(CYCLE_DOT))
        assert longest_chain(pn) == []

    def test_empty_returns_empty(self) -> None:
        assert longest_chain(nx.DiGraph()) == []


# ===========================================================================
# find_cycle
# ===========================================================================


class TestFindCycle:
    def test_acyclic_reports_none(self, dot_text: str) -> None:
        """The falsifier: cycle detection reports none for an acyclic graph."""
        pn = collapse_to_pn(read_graph(dot_text))
        assert find_cycle(pn) == []

    def test_seeded_cycle_found(self) -> None:
        """The falsifier: cycle detection finds the seeded cycle."""
        pn = collapse_to_pn(read_graph(CYCLE_DOT))
        names = find_cycle(pn)
        assert set(names) == {"a", "b", "c"}

    def test_empty_graph_reports_none(self) -> None:
        assert find_cycle(nx.DiGraph()) == []


# ===========================================================================
# critical_nodes
# ===========================================================================


class TestCriticalNodes:
    def test_highest_in_degree_first(self, dot_text: str) -> None:
        pn = collapse_to_pn(read_graph(dot_text))
        ranked = critical_nodes(pn)
        assert ranked[0] == ("gcc-cross", 2)

    def test_zero_in_degree_omitted(self, dot_text: str) -> None:
        pn = collapse_to_pn(read_graph(dot_text))
        names = [n for n, _ in critical_nodes(pn)]
        assert "busybox" not in names

    def test_top_n_limit(self, dot_text: str) -> None:
        pn = collapse_to_pn(read_graph(dot_text))
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
        assert result["longest_chain"][0] == "busybox"
        assert "direct_deps" in result
        assert isinstance(result["direct_deps"], list)

    def test_depth_propagates(self, dot_text: str, buildlist_text: str) -> None:
        result = analyze(dot_text, buildlist_text, "busybox", depth=1)
        assert result["blast_radius"] == 2
        assert result["depth"] == 1

    def test_empty_dot_does_not_crash(self, buildlist_text: str) -> None:
        result = analyze("", buildlist_text, "busybox")
        assert result["blast_radius"] == 0
        assert result["cycle"] == []
