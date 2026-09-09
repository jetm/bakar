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

from typing import TYPE_CHECKING

from bakar.buildstats import (
    latest_capture,
    parse_task_file,
    read_run,
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


def _capture(tmp_path: Path, ts: str = "20260909123055") -> Path:
    d = tmp_path / "buildstats" / ts
    d.mkdir(parents=True)
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
