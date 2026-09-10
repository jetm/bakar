"""Tests for the offline dependency source `bakar insights` reads.

The point of this source is that the critical path - and with it the
concurrency floor - becomes computable from a persisted run directory, where it
previously required a live `bitbake -g <recipe>`.

The property under test is provenance. A graph sitting in a run directory is
not evidence that it describes that run, and the failure it guards is the same
shape the buildstats join gate guards: consecutive builds of one target produce
near-identical graphs, so a mis-correlated one does not look wrong. It looks
like an answer.

The headline falsifier: a graph captured before the run even started must be
refused. A source that checked only for the file's presence would accept it and
publish a confident critical path for a build it does not describe.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from bakar.commands.insights import GRAPH_CORRELATION_TOLERANCE_S, _dependency_source
from bakar.steps import kas_build

if TYPE_CHECKING:
    from pathlib import Path

DOT = 'digraph depends {\n"a.do_compile" -> "b.do_populate_sysroot"\n}\n'
BUILDLIST = "a\nb\n"

# A run that started at 1000 and finished at 2000.
WINDOW = (1000.0, 2000.0)


def _capture(run_dir: Path, captured_at: float, *, marker: bool = True, artifacts: bool = True) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    if artifacts:
        (run_dir / "task-depends.dot").write_text(DOT)
        (run_dir / "pn-buildlist").write_text(BUILDLIST)
    if marker:
        (run_dir / kas_build.GRAPH_MARKER_NAME).write_text(
            json.dumps({"target": "core-image-minimal", "captured_at": captured_at, "artifacts": {}})
        )


def test_a_correlated_graph_is_returned(tmp_path: Path) -> None:
    # Captured just after the build finished, which is where capture runs.
    _capture(tmp_path, 2010.0)

    dot, buildlist = _dependency_source(tmp_path, WINDOW)()

    assert dot == DOT
    assert buildlist == BUILDLIST


def test_a_graph_captured_before_the_run_started_is_refused(tmp_path: Path) -> None:
    """The headline falsifier: a previous build's graph in this run's directory."""
    _capture(tmp_path, 500.0)

    with pytest.raises(RuntimeError, match="does not belong to this run"):
        _dependency_source(tmp_path, WINDOW)()


def test_a_graph_captured_long_after_the_run_is_refused(tmp_path: Path) -> None:
    _capture(tmp_path, 2000.0 + GRAPH_CORRELATION_TOLERANCE_S + 1)

    with pytest.raises(RuntimeError, match="does not belong to this run"):
        _dependency_source(tmp_path, WINDOW)()


def test_capture_within_the_tolerance_after_the_build_is_accepted(tmp_path: Path) -> None:
    """A loaded machine can take a while between last task and marker write.

    The tolerance exists to reject a DIFFERENT build's graph, which is hours
    away, not to police the seconds after a build's last task.
    """
    _capture(tmp_path, 2000.0 + GRAPH_CORRELATION_TOLERANCE_S - 1)

    dot, _ = _dependency_source(tmp_path, WINDOW)()

    assert dot == DOT


def test_no_captured_graph_says_so(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="no dependency graph captured"):
        _dependency_source(tmp_path, WINDOW)()


def test_a_run_with_no_window_cannot_correlate(tmp_path: Path) -> None:
    """Without a build window nothing can be shown to belong to the run.

    This must not silently accept the graph - an uncorrelatable capture and a
    correlated one call for different confidence in every number derived from
    it.
    """
    _capture(tmp_path, 2010.0)

    with pytest.raises(RuntimeError, match="cannot be shown to belong to it"):
        _dependency_source(tmp_path, None)()


def test_a_marker_with_no_capture_time_is_refused(tmp_path: Path) -> None:
    _capture(tmp_path, 2010.0)
    (tmp_path / kas_build.GRAPH_MARKER_NAME).write_text(json.dumps({"target": "x"}))

    with pytest.raises(RuntimeError, match="records no capture time"):
        _dependency_source(tmp_path, WINDOW)()


def test_an_unparseable_marker_is_refused(tmp_path: Path) -> None:
    _capture(tmp_path, 2010.0)
    (tmp_path / kas_build.GRAPH_MARKER_NAME).write_text("{not json")

    with pytest.raises(RuntimeError, match="unreadable"):
        _dependency_source(tmp_path, WINDOW)()


def test_a_bare_graph_with_no_marker_is_refused(tmp_path: Path) -> None:
    """Artifacts without provenance are exactly what co-location cannot vouch for."""
    _capture(tmp_path, 2010.0, marker=False)

    with pytest.raises(RuntimeError, match="no dependency graph captured"):
        _dependency_source(tmp_path, WINDOW)()


def test_a_correlated_marker_with_missing_artifacts_is_refused(tmp_path: Path) -> None:
    _capture(tmp_path, 2010.0, artifacts=False)

    with pytest.raises(RuntimeError, match="incomplete"):
        _dependency_source(tmp_path, WINDOW)()
