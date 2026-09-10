"""Tests for :mod:`bakar.buildstats`.

The contract under test is mostly about two distinctions that a careless reader
folds together:

* **absent vs empty.** A tree that was never found and a build that recorded
  nothing are opposite answers - a path problem versus a real measurement - and
  both yield zero tasks. Callers branch on ``outcome``, so it has to be right.
* **``rusage ru_utime`` vs bare ``utime``.** A task file carries both; the first
  is seconds and the second is clock ticks. Reading the wrong one inflates CPU
  by roughly two orders of magnitude and makes the CPU floor look like the
  binding constraint on every build.

The headline falsifier: a fixture whose ``utime`` and ``rusage ru_utime`` differ
by ~100x must parse to the rusage value. A reader that picked the tick field
would pass every other test here.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from bakar.buildstats import (
    capture_epochs,
    latest_capture,
    parse_task_file,
    read_run,
    select_capture,
)

if TYPE_CHECKING:
    from pathlib import Path

# A real do_compile record, trimmed. Note utime=16 (ticks) beside
# rusage ru_utime=0.118937 (seconds) - the distinction this module exists to
# get right.
SAMPLE = """\
Event: TaskStarted
Started: 1788658832.86
acl-2.3.2-r0: do_compile
Elapsed time: 1.48 seconds
utime: 16
stime: 2
cutime: 430
cstime: 107
IO syscr: 24404
IO syscw: 5562
IO write_bytes: 1871872
rusage ru_utime: 0.118937
rusage ru_stime: 0.020840
rusage ru_minflt: 8841
rusage ru_majflt: 0
Child rusage ru_utime: 4.303937
Child rusage ru_stime: 1.076686
Child rusage ru_minflt: 555250
Child rusage ru_majflt: 2
Status: PASSED
Ended: 1788658834.34
"""


def _stamp_epoch(ts: str) -> float:
    """Interpret a ``YYYYMMDDHHMMSS`` test stamp as UTC.

    Deliberately its own parser rather than a call to ``capture_epochs``: that
    function is the thing under test, so using it here would define the expected
    window in terms of what it returns.
    """
    return datetime.strptime(ts, "%Y%m%d%H%M%S").replace(tzinfo=UTC).timestamp()


def _capture(tmp_path: Path, ts: str = "20260909123055") -> Path:
    """Create a capture directory whose MTIME matches its name.

    Correlation reads mtime, not the name, because the name carries no timezone
    and the build container's is not knowable from here. Setting it explicitly
    is what makes these fixtures mean what their names say - and a test that
    only created the directory would correlate against whenever the suite
    happened to run.
    """
    d = tmp_path / "buildstats" / ts
    d.mkdir(parents=True)
    epoch = _stamp_epoch(ts)
    os.utime(d, (epoch, epoch))
    return d


def _task(capture: Path, recipe: str, task: str, body: str = SAMPLE) -> Path:
    rd = capture / recipe
    rd.mkdir(parents=True, exist_ok=True)
    p = rd / task
    p.write_text(body)
    return p


def test_cpu_comes_from_rusage_seconds_not_tick_counters(tmp_path: Path) -> None:
    """The headline falsifier. utime=16 ticks vs ru_utime=0.118937 seconds."""
    cap = _capture(tmp_path)
    _task(cap, "acl-2.3.2-r0", "do_compile")

    run = read_run(tmp_path)

    assert run.outcome == "parsed"
    t = run.tasks[0]
    # 0.118937 + 0.020840 + 4.303937 + 1.076686
    assert abs(t.cpu_seconds - 5.5204) < 0.001
    # The tick sum would be 16+2+430+107 = 555, ~100x larger.
    assert t.cpu_seconds < 10


def test_fault_and_syscall_counters_sum_task_and_children(tmp_path: Path) -> None:
    cap = _capture(tmp_path)
    _task(cap, "acl-2.3.2-r0", "do_compile")

    t = read_run(tmp_path).tasks[0]

    assert t.minflt == 8841 + 555250
    assert t.majflt == 0 + 2
    assert t.syscalls == 24404 + 5562
    assert t.write_bytes == 1871872


def test_absent_tree_is_not_the_same_as_an_empty_one(tmp_path: Path) -> None:
    """The distinction the whole module exists to preserve."""
    run = read_run(tmp_path)

    assert run.outcome == "absent"
    assert run.tasks == []


def test_tree_with_no_capture_directories_is_empty_not_absent(tmp_path: Path) -> None:
    (tmp_path / "buildstats").mkdir()

    run = read_run(tmp_path)

    assert run.outcome == "empty"
    assert "no capture directories" in run.note


def test_capture_with_no_parseable_records_is_empty_not_absent(tmp_path: Path) -> None:
    cap = _capture(tmp_path)
    _task(cap, "acl-2.3.2-r0", "do_compile", body="Event: TaskStarted\nStatus: PASSED\n")

    run = read_run(tmp_path)

    assert run.outcome == "empty"
    assert run.directory == cap
    assert "no parseable task records" in run.note


def test_bitbakes_own_summary_file_is_not_a_task(tmp_path: Path) -> None:
    """``build_stats`` sits in the capture root and parses into a bogus row."""
    cap = _capture(tmp_path)
    (cap / "build_stats").write_text(SAMPLE)
    _task(cap, "acl-2.3.2-r0", "do_compile")

    run = read_run(tmp_path)

    assert [t.task for t in run.tasks] == ["do_compile"]


def test_a_record_with_no_elapsed_time_is_dropped(tmp_path: Path) -> None:
    """A half-written record must be omitted, not defaulted to zero duration."""
    cap = _capture(tmp_path)
    _task(cap, "good-1.0-r0", "do_compile")
    _task(cap, "partial-1.0-r0", "do_compile", body="rusage ru_utime: 1.0\n")

    run = read_run(tmp_path)

    assert [t.recipe for t in run.tasks] == ["good-1.0-r0"]


def test_latest_capture_picks_the_newest_of_several(tmp_path: Path) -> None:
    """A build directory accumulates one capture per run - 27 on one real tree."""
    for ts in ("20260101000000", "20260909123055", "20260501000000"):
        _capture(tmp_path, ts)

    assert latest_capture(tmp_path).name == "20260909123055"


def test_latest_capture_on_an_absent_tree_is_none(tmp_path: Path) -> None:
    assert latest_capture(tmp_path) is None


def test_unreadable_task_file_does_not_raise(tmp_path: Path) -> None:
    """A tree is written concurrently with the build; truncation is ordinary."""
    assert parse_task_file(tmp_path / "nope") == {}


def test_malformed_numeric_lines_are_skipped(tmp_path: Path) -> None:
    cap = _capture(tmp_path)
    _task(
        cap,
        "acl-2.3.2-r0",
        "do_compile",
        body="Elapsed time: 1.48 seconds\nrusage ru_utime: not-a-number\nrusage ru_stime: 2.0\n",
    )

    t = read_run(tmp_path).tasks[0]

    assert t.cpu_seconds == 2.0


def test_totals_aggregate_across_tasks(tmp_path: Path) -> None:
    cap = _capture(tmp_path)
    _task(cap, "a-1.0-r0", "do_compile")
    _task(cap, "b-1.0-r0", "do_compile")

    run = read_run(tmp_path)

    assert len(run.tasks) == 2
    assert abs(run.total_cpu_seconds - 2 * 5.5204) < 0.002
    assert abs(run.total_elapsed_seconds - 2 * 1.48) < 0.001


# --- capture correlation ----------------------------------------------------
#
# ``latest_capture`` answers "what ran last", which is the wrong question for a
# caller that named a run. A build directory accumulates one capture per run, so
# reporting on an older run joins it against a later build's records - and
# because consecutive builds of one target execute a near-identical (PN, task)
# set, that join clears the 95% gate at close to 100%. The gate then passes over
# exactly the provenance failure it exists to catch.


def _window(started: str, completed: str) -> tuple[float, float]:
    """Build an epoch window from two local ``YYYYMMDDHHMMSS`` stamps."""
    return (_stamp_epoch(started), _stamp_epoch(completed))


def test_capture_is_chosen_by_the_runs_own_window_not_by_recency(tmp_path: Path) -> None:
    mine = _capture(tmp_path, "20260909123055")
    _task(mine, "acl-2.3.2-r0", "do_compile")
    later = _capture(tmp_path, "20260910080000")
    _task(later, "acl-2.3.2-r0", "do_compile")

    assert latest_capture(tmp_path) == later
    assert select_capture(tmp_path, _window("20260909123000", "20260909130000")) == mine

    run = read_run(tmp_path, window=_window("20260909123000", "20260909130000"))
    assert run.outcome == "parsed"
    assert run.directory == mine


def test_no_capture_in_the_window_is_uncorrelated_not_absent_or_empty(tmp_path: Path) -> None:
    """A fourth outcome, because it calls for a fourth response.

    "absent" sends a reader after the path, "empty" says this build recorded
    nothing, and neither is true of a tree full of some other build's captures.
    """
    other = _capture(tmp_path, "20260910080000")
    _task(other, "acl-2.3.2-r0", "do_compile")

    run = read_run(tmp_path, window=_window("20260909123000", "20260909130000"))

    assert run.outcome == "uncorrelated"
    assert run.tasks == []
    assert run.directory is None


def test_a_capture_directory_with_a_foreign_name_never_correlates(tmp_path: Path) -> None:
    """Only a ``YYYYMMDDHHMMSS`` name can be placed in time at all."""
    stray = tmp_path / "buildstats" / "scratch"
    stray.mkdir(parents=True)
    _task(stray, "acl-2.3.2-r0", "do_compile")

    assert capture_epochs(stray) == ()
    assert read_run(tmp_path, window=_window("20260909123000", "20260909130000")).outcome == "uncorrelated"


def test_omitting_the_window_keeps_the_newest_capture_behaviour(tmp_path: Path) -> None:
    """Still the right answer for a caller that genuinely wants whatever ran last."""
    _task(_capture(tmp_path, "20260909123055"), "acl-2.3.2-r0", "do_compile")
    later = _capture(tmp_path, "20260910080000")
    _task(later, "acl-2.3.2-r0", "do_compile")

    assert read_run(tmp_path).directory == later


def test_two_captures_inside_the_slack_resolve_to_the_nearer_start(tmp_path: Path) -> None:
    """The tolerance must not reintroduce the wrong-build join it sits beside.

    A rebuild two minutes later falls inside the earlier run's window once the
    300s slack is applied. Taking the last match then hands the earlier run the
    later build's capture, which is precisely what correlating was for.
    """
    mine = _capture(tmp_path, "20260909123055")
    _task(mine, "acl-2.3.2-r0", "do_compile")
    rebuild = _capture(tmp_path, "20260909123300")
    _task(rebuild, "acl-2.3.2-r0", "do_compile")

    assert select_capture(tmp_path, _window("20260909123055", "20260909123145")) == mine
    assert select_capture(tmp_path, _window("20260909123300", "20260909123400")) == rebuild
