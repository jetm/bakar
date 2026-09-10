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

import math
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from bakar.buildstats import (
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

    Deliberately its own parser rather than a call into the module: capture
    correlation is what these tests exercise, so deriving the expected window
    from it would define the expectation in terms of the thing under test.
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


def test_a_malformed_cpu_line_makes_the_whole_record_incomplete(tmp_path: Path) -> None:
    """Updated from an assertion that this parsed to ``cpu_seconds == 2.0``.

    That encoded the f-0001 defect: an unparseable ``ru_utime`` fell through
    ``.get(key, 0.0)`` and counted as zero CPU, so the record stayed valid,
    joined, and understated the build's CPU seconds under a gate that reads
    100%. A record missing any CPU component is now dropped instead.
    """
    cap = _capture(tmp_path)
    _task(cap, "good-1.0-r0", "do_compile")
    _task(
        cap,
        "acl-2.3.2-r0",
        "do_compile",
        body="Elapsed time: 1.48 seconds\nrusage ru_utime: not-a-number\nrusage ru_stime: 2.0\n",
    )

    run = read_run(tmp_path)

    assert [t.recipe for t in run.tasks] == ["good-1.0-r0"]
    assert "1 incomplete or invalid records skipped" in run.note


def test_a_malformed_churn_line_leaves_the_record_intact(tmp_path: Path) -> None:
    """Churn is deliberately not held to the CPU rule.

    A zero major-fault count is a real measurement - plenty of tasks record one -
    so absence and zero are not distinguishable there the way they are for CPU,
    and requiring the counters would drop real records over a column that only
    describes.
    """
    cap = _capture(tmp_path)
    _task(
        cap,
        "acl-2.3.2-r0",
        "do_compile",
        body=(
            "Elapsed time: 1.48 seconds\n"
            "rusage ru_utime: 1.0\n"
            "rusage ru_stime: 1.0\n"
            "Child rusage ru_utime: 0.0\n"
            "Child rusage ru_stime: 0.0\n"
            "rusage ru_minflt: not-a-number\n"
        ),
    )

    t = read_run(tmp_path).tasks[0]

    assert t.cpu_seconds == 2.0
    assert t.minflt == 0


def test_a_record_with_elapsed_but_no_cpu_is_not_a_zero_cpu_task(tmp_path: Path) -> None:
    """bitbake writes ``Elapsed time`` before the ``rusage`` block.

    A file read mid-build therefore carries a duration and no CPU at all.
    Defaulting the CPU components to zero made that a valid record which JOINED,
    letting the 95% gate pass while the CPU floor it gates silently understated
    the build. A task that consumed no CPU does not exist, so the zero is always
    a record bitbake had not finished writing.
    """
    cap = _capture(tmp_path)
    _task(cap, "good-1.0-r0", "do_compile")
    _task(
        cap,
        "half-written-1.0-r0",
        "do_compile",
        body="Event: TaskStarted\nElapsed time: 12.00 seconds\nutime: 16\nstime: 2\n",
    )

    run = read_run(tmp_path)

    assert [t.recipe for t in run.tasks] == ["good-1.0-r0"]


def test_a_record_truncated_before_the_child_rusage_block_is_incomplete(tmp_path: Path) -> None:
    """``Child rusage`` lands last, and it carries the bulk of the CPU.

    bitbake forks the real work out to compilers and shells, so a record whose
    own rusage arrived but whose children's did not understates CPU by roughly
    the whole task.
    """
    cap = _capture(tmp_path)
    _task(
        cap,
        "acl-2.3.2-r0",
        "do_compile",
        body="Elapsed time: 1.48 seconds\nrusage ru_utime: 0.118937\nrusage ru_stime: 0.020840\n",
    )

    assert read_run(tmp_path).outcome == "empty"


def test_a_negative_cpu_component_never_reaches_a_record(tmp_path: Path) -> None:
    """A negative CPU component yields a negative CPU floor - enormous headroom."""
    cap = _capture(tmp_path)
    _task(cap, "good-1.0-r0", "do_compile")
    _task(
        cap,
        "impossible-1.0-r0",
        "do_compile",
        body=(
            "Elapsed time: 1.48 seconds\n"
            "rusage ru_utime: -5000.0\n"
            "rusage ru_stime: 1.0\n"
            "Child rusage ru_utime: 1.0\n"
            "Child rusage ru_stime: 1.0\n"
        ),
    )

    run = read_run(tmp_path)

    assert [t.recipe for t in run.tasks] == ["good-1.0-r0"]
    assert run.total_cpu_seconds > 0


def test_non_finite_numbers_never_reach_a_record(tmp_path: Path) -> None:
    """``float()`` takes ``nan`` and ``inf`` as ordinary literals.

    ``nan`` propagates into the floor and the headroom percentage and renders as
    ``nan`` rather than as a refusal; ``int(inf)`` raises out of the whole read
    and discards every other record with it.
    """
    cap = _capture(tmp_path)
    _task(cap, "good-1.0-r0", "do_compile")
    _task(
        cap,
        "nan-1.0-r0",
        "do_compile",
        body=(
            "Elapsed time: 1.48 seconds\n"
            "rusage ru_utime: nan\n"
            "rusage ru_stime: 1.0\n"
            "Child rusage ru_utime: 1.0\n"
            "Child rusage ru_stime: 1.0\n"
        ),
    )
    _task(
        cap,
        "inf-1.0-r0",
        "do_compile",
        body=(
            "Elapsed time: 1.48 seconds\n"
            "rusage ru_utime: 1.0\n"
            "rusage ru_stime: 1.0\n"
            "Child rusage ru_utime: 1.0\n"
            "Child rusage ru_stime: 1.0\n"
            "rusage ru_minflt: inf\n"
        ),
    )

    run = read_run(tmp_path)

    assert run.outcome == "parsed"
    # nan is CPU-incomplete and dropped; the inf lands on a churn counter, so
    # that record survives with the counter absent rather than aborting the read.
    assert sorted(t.recipe for t in run.tasks) == ["good-1.0-r0", "inf-1.0-r0"]
    assert all(t.minflt >= 0 for t in run.tasks)
    assert math.isfinite(run.total_cpu_seconds)


def test_an_unreadable_recipe_directory_is_skipped_not_raised(tmp_path: Path) -> None:
    """The tree is written concurrently with the build, so a directory can go.

    ``parse_task_file`` and ``latest_capture`` both guard their own reads for
    this reason; ``read_run``'s two-level walk did not, so a rotation mid-walk
    raised out of it instead of degrading (design D2).
    """
    cap = _capture(tmp_path)
    _task(cap, "good-1.0-r0", "do_compile")
    locked = cap / "locked-1.0-r0"
    locked.mkdir()
    (locked / "do_compile").write_text(SAMPLE)
    locked.chmod(0o000)
    if os.access(locked, os.R_OK):  # running as root - the mode says nothing
        locked.chmod(0o755)
        pytest.skip("cannot make a directory unreadable as root")
    try:
        run = read_run(tmp_path)
    finally:
        locked.chmod(0o755)

    assert run.outcome == "parsed"
    assert [t.recipe for t in run.tasks] == ["good-1.0-r0"]
    assert "1 recipe directories unreadable mid-walk" in run.note


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
    """Only a ``YYYYMMDDHHMMSS`` name can be placed in time at all.

    Not even by mtime: a scratch directory touched during the build would
    otherwise read as this run's provenance, and the name check is the only
    thing standing between the two.
    """
    stray = tmp_path / "buildstats" / "scratch"
    stray.mkdir(parents=True)
    _task(stray, "acl-2.3.2-r0", "do_compile")
    _retime(stray, "20260909123100")

    window = _window("20260909123000", "20260909130000")
    assert select_capture(tmp_path, window) is None
    assert read_run(tmp_path, window=window).outcome == "uncorrelated"


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


def _retime(capture: Path, ts: str) -> Path:
    """Move a capture's MTIME away from its name.

    Every other fixture here keeps the two agreeing, which is exactly the case
    that cannot detect a reader treating them as interchangeable. Call this
    AFTER writing the capture's task files - creating a recipe subdirectory
    bumps the parent's mtime, which is why the real reading lands near a build's
    end in the first place.
    """
    epoch = _stamp_epoch(ts)
    os.utime(capture, (epoch, epoch))
    return capture


def test_the_name_outranks_a_previous_builds_mtime(tmp_path: Path) -> None:
    """The two readings are not interchangeable and rank in tiers, not together.

    The NAME is the build's start; the MTIME is near its end, because bitbake
    bumps the parent every time it writes a recipe subdirectory (measured on a
    real capture: name 14:20:35 against mtime 14:26:30). Pooling them and taking
    whichever reading sits closest to this run's start therefore prefers the
    PREVIOUS build - its end lands nearer this run's start than this run's own
    name does, once the 300s slack pulls it inside the window. With a
    near-identical task set the join gate then clears at ~100% over another
    build's records, which is the whole failure correlating was added to stop.
    """
    previous = _capture(tmp_path, "20260909120000")
    _task(previous, "acl-2.3.2-r0", "do_compile")
    _retime(previous, "20260909122955")
    mine = _capture(tmp_path, "20260909123055")
    _task(mine, "zlib-1.3-r0", "do_compile")
    _retime(mine, "20260909123600")

    window = _window("20260909123000", "20260909123600")

    # Ranking the pooled readings puts the previous build's mtime (45s from the
    # start) ahead of this run's own name (55s from it).
    assert select_capture(tmp_path, window) == mine
    assert [t.recipe for t in read_run(tmp_path, window=window).tasks] == ["zlib-1.3-r0"]


def test_a_lone_mtime_match_still_correlates(tmp_path: Path) -> None:
    """The mtime tier is what covers a host whose bitbake writes local time.

    Its name is then hours out of the window and only the mtime can place it.
    Dropping the fallback would make every such fleet's floor refuse forever.
    """
    local = _capture(tmp_path, "20260909063055")
    _task(local, "acl-2.3.2-r0", "do_compile")
    _retime(local, "20260909123100")

    run = read_run(tmp_path, window=_window("20260909123000", "20260909123600"))

    assert run.outcome == "parsed"
    assert run.directory == local


def test_two_captures_matching_only_by_mtime_refuse_rather_than_guess(tmp_path: Path) -> None:
    """Ambiguous provenance is refused, not ranked (design D5).

    mtime is near a build's END, so "closest to this run's start" is not even
    the right ordering for it - the nearest mtime is the build that finished
    just before this one began.
    """
    first = _capture(tmp_path, "20260101000000")
    _task(first, "acl-2.3.2-r0", "do_compile")
    _retime(first, "20260909123100")
    second = _capture(tmp_path, "20260102000000")
    _task(second, "zlib-1.3-r0", "do_compile")
    _retime(second, "20260909123500")

    window = _window("20260909123000", "20260909123600")

    assert select_capture(tmp_path, window) is None
    assert read_run(tmp_path, window=window).outcome == "uncorrelated"


def test_an_empty_tree_is_empty_even_when_a_window_is_supplied(tmp_path: Path) -> None:
    """An empty tree and a foreign-capture tree are different answers.

    ``select_capture`` returns None for both, and reading ``uncorrelated`` off
    that bare None told a reader some other build's captures were sitting there
    when the directory held nothing at all - collapsing the absence-vs-emptiness
    distinction the spec requires as separate outcomes.
    """
    (tmp_path / "buildstats").mkdir()

    run = read_run(tmp_path, window=_window("20260909123000", "20260909130000"))

    assert run.outcome == "empty"
    assert "no capture directories" in run.note
