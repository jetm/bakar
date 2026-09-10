"""Tests for :mod:`bakar.insights_timing`.

Covers duration computation from started/completed timestamps, the
missing-``completed`` skip rule, top-N truncation/padding for both more-than-N
and fewer-than-N completed tasks, and the critical-path opt-in section's
graceful degradation when the dependency source is unavailable versus when it
succeeds. The critical-path tests use a scripted stand-in callable for
``dependency_source`` rather than a real ``bitbake -g`` invocation, so the
suite stays hermetic.
"""

from __future__ import annotations

import json
import math
import re
from typing import TYPE_CHECKING

import pytest

from bakar import eventlog
from bakar.buildstats import BuildstatsRun, TaskStats
from bakar.insights_timing import TimingReport, timing_report

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


def _row(recipe: str, task: str, started: float, completed: float | None) -> dict:
    row = {"task": task, "recipe": recipe, "started": started}
    if completed is not None:
        row["completed"] = completed
    return row


@pytest.mark.unit
def test_duration_computed_from_timestamps(tmp_path: Path) -> None:
    artifact = {"tasks": [_row("busybox", "do_compile", 100.0, 142.5)]}

    report = timing_report(artifact, baselines_path=tmp_path / "absent.json")

    assert len(report.top_slowest) == 1
    duration = report.top_slowest[0]
    assert duration.recipe == "busybox"
    assert duration.task == "do_compile"
    assert duration.duration == pytest.approx(42.5)


@pytest.mark.unit
def test_task_missing_completed_is_excluded_without_raising(tmp_path: Path) -> None:
    artifact = {
        "tasks": [
            _row("busybox", "do_compile", 100.0, 142.5),
            _row("zlib", "do_fetch", 200.0, None),
        ]
    }

    report = timing_report(artifact, baselines_path=tmp_path / "absent.json")

    assert [d.recipe for d in report.top_slowest] == ["busybox"]


@pytest.mark.unit
def test_top_n_truncates_when_more_than_n_completed_tasks(tmp_path: Path) -> None:
    artifact = {"tasks": [_row(f"recipe{i}", "do_compile", 0.0, float(i)) for i in range(1, 6)]}

    report = timing_report(artifact, top_n=3, baselines_path=tmp_path / "absent.json")

    assert len(report.top_slowest) == 3
    # Sorted descending by duration: recipe5 (5s), recipe4 (4s), recipe3 (3s).
    assert [d.recipe for d in report.top_slowest] == ["recipe5", "recipe4", "recipe3"]


@pytest.mark.unit
def test_top_n_unpadded_when_fewer_than_n_completed_tasks(tmp_path: Path) -> None:
    artifact = {
        "tasks": [
            _row("busybox", "do_compile", 0.0, 5.0),
            _row("zlib", "do_compile", 0.0, 3.0),
        ]
    }

    report = timing_report(artifact, top_n=10, baselines_path=tmp_path / "absent.json")

    assert len(report.top_slowest) == 2
    assert [d.recipe for d in report.top_slowest] == ["busybox", "zlib"]


@pytest.mark.unit
def test_critical_path_unavailable_when_dependency_source_raises(tmp_path: Path) -> None:
    artifact = {"tasks": [_row("busybox", "do_compile", 0.0, 42.0)]}

    def _broken_source() -> tuple[str, str]:
        raise RuntimeError("bitbake -g unavailable in this environment")

    report = timing_report(
        artifact,
        baselines_path=tmp_path / "absent.json",
        dependency_source=_broken_source,
    )

    # Duration and top-N sections must stay populated even though the
    # dependency model failed - the falsifier this test guards against is a
    # regression that empties them on critical-path failure.
    assert len(report.top_slowest) == 1
    assert report.top_slowest[0].duration == pytest.approx(42.0)

    assert report.critical_path.available is False
    assert report.critical_path.chain == []
    assert report.critical_path.total_seconds == 0.0
    assert "unavailable" in report.critical_path.note


@pytest.mark.unit
def test_critical_path_unavailable_when_dependency_source_returns_empty_graph(
    tmp_path: Path,
) -> None:
    artifact = {"tasks": [_row("busybox", "do_compile", 0.0, 42.0)]}

    def _empty_source() -> tuple[str, str]:
        return "digraph { }", ""

    report = timing_report(
        artifact,
        baselines_path=tmp_path / "absent.json",
        dependency_source=_empty_source,
    )

    assert len(report.top_slowest) == 1
    assert report.critical_path.available is False
    assert "unavailable" in report.critical_path.note


@pytest.mark.unit
def test_critical_path_available_when_dependency_source_succeeds(tmp_path: Path) -> None:
    artifact = {
        "tasks": [
            _row("a", "do_compile", 0.0, 10.0),
            _row("b", "do_compile", 0.0, 20.0),
        ]
    }

    def _valid_source() -> tuple[str, str]:
        return 'digraph { "a.do_compile" -> "b.do_compile"; }', ""

    report = timing_report(
        artifact,
        baselines_path=tmp_path / "absent.json",
        dependency_source=_valid_source,
    )

    assert len(report.top_slowest) == 2
    assert report.critical_path.available is True
    assert report.critical_path.chain == ["a", "b"]
    assert report.critical_path.total_seconds == pytest.approx(30.0)


@pytest.mark.unit
def test_critical_path_credits_the_head_nodes_own_duration(tmp_path: Path) -> None:
    """A path's first node has no incoming edge - its own duration must still
    count when comparing it against a competing chain, or a long head node
    followed by cheap successors loses to an unrelated short-head/expensive-tail
    chain purely because edge-weighting only ever credits destination nodes."""
    artifact = {
        "tasks": [
            _row("a", "do_compile", 0.0, 100.0),  # head of the true-longest chain
            _row("b", "do_compile", 100.0, 101.0),
            _row("c", "do_compile", 0.0, 10.0),
            _row("d", "do_compile", 10.0, 60.0),  # 50s task, but not the longest true chain
        ]
    }

    def _two_chain_source() -> tuple[str, str]:
        return 'digraph { "a.do_compile" -> "b.do_compile"; "c.do_compile" -> "d.do_compile"; }', ""

    report = timing_report(
        artifact,
        baselines_path=tmp_path / "absent.json",
        dependency_source=_two_chain_source,
    )

    assert report.critical_path.available is True
    assert report.critical_path.chain == ["a", "b"]
    assert report.critical_path.total_seconds == pytest.approx(101.0)


def _task(recipe: str, task: str, start: float, end: float) -> dict:
    return {"recipe": recipe, "task": task, "started": start, "completed": end}


def test_regime_reports_the_restored_share(tmp_path: Path) -> None:
    artifact = {
        "tasks": [_task("busybox", "do_compile", 0.0, 5.0)],
        "setscene": {"covered": 786, "notcovered": 999, "total": 2295},
    }

    report = timing_report(artifact, baselines_path=tmp_path / "absent.json")

    assert report.regime.measured is True
    assert report.regime.covered == 786
    assert report.regime.covered_pct == pytest.approx(34.2, abs=0.1)
    assert "34.2% restored" in report.regime.note


def test_zero_covered_with_tasks_is_a_cold_build(tmp_path: Path) -> None:
    """Nothing restored is a real regime, and must read as measured."""
    artifact = {
        "tasks": [_task("busybox", "do_compile", 0.0, 5.0)],
        "setscene": {"covered": 0, "notcovered": 2023, "total": 2023},
    }

    report = timing_report(artifact, baselines_path=tmp_path / "absent.json")

    assert report.regime.measured is True
    assert "cold" in report.regime.note


def test_all_zero_setscene_with_no_tasks_is_unmeasured_not_cold(tmp_path: Path) -> None:
    """The headline falsifier for this section.

    ``bakar.eventlog`` returns a zero-seeded setscene block verbatim when
    bitbake's raw log is missing, so zeros alone cannot be read as a cold
    build. An empty task list is the discriminator; without it an unmeasured
    run is recorded as a measured cold one, on the success path.
    """
    artifact = {"tasks": [], "setscene": {"covered": 0, "notcovered": 0, "total": 0}}

    report = timing_report(artifact, baselines_path=tmp_path / "absent.json")

    assert report.regime.measured is False
    assert "unmeasured" in report.regime.note


def test_artifact_without_a_setscene_block_is_unmeasured(tmp_path: Path) -> None:
    artifact = {"tasks": [_task("busybox", "do_compile", 0.0, 5.0)]}

    report = timing_report(artifact, baselines_path=tmp_path / "absent.json")

    assert report.regime.measured is False
    assert "no setscene block" in report.regime.note


def test_bare_tasks_list_reports_unmeasured_rather_than_guessing(tmp_path: Path) -> None:
    """A list carries no setscene block, so there is nothing to read."""
    report = timing_report([_task("busybox", "do_compile", 0.0, 5.0)], baselines_path=tmp_path / "absent.json")

    assert report.regime.measured is False
    assert "tasks list only" in report.regime.note


def test_malformed_setscene_counts_do_not_raise(tmp_path: Path) -> None:
    artifact = {
        "tasks": [_task("busybox", "do_compile", 0.0, 5.0)],
        "setscene": {"covered": "many", "notcovered": None, "total": 10},
    }

    report = timing_report(artifact, baselines_path=tmp_path / "absent.json")

    assert report.regime.covered == 0
    assert report.regime.total == 10


# --- buildstats join gate -------------------------------------------------
#
# Two failures are specifically under test here, because both are silent.
# A refused floor whose number still reaches the page is taken and its caveat
# discarded; a rate computed by dropping unjoined tasks from BOTH sides reads
# 100% forever and passes the gate over exactly the partial join it exists to
# catch. The first is checked by scanning rendered text for any duration-shaped
# token, the second by asserting a deliberately partial join reports its real
# partial value.

#: Any number immediately followed by a time unit. Deliberately broad: the
#: assertion is that NO duration reaches a refused section, so a pattern that
#: only matched this module's own formatting would pass a section that spelled
#: its seconds differently.
DURATION_TOKEN = re.compile(r"\d+(?:\.\d+)?\s*(?:s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hr|hrs)\b")


def _stat(recipe: str, task: str, cpu_seconds: float) -> TaskStats:
    return TaskStats(
        recipe=recipe,
        task=task,
        elapsed=cpu_seconds,
        cpu_seconds=cpu_seconds,
        minflt=0,
        majflt=0,
        syscalls=0,
        write_bytes=0,
    )


def _parsed(*stats: TaskStats) -> BuildstatsRun:
    return BuildstatsRun(outcome="parsed", note="fixture", tasks=list(stats))


def test_join_rate_counts_unjoined_tasks_in_the_denominator(tmp_path: Path) -> None:
    """Four executed tasks, two with records: the rate is 50%, not 100%.

    The trap this guards is a join built by iterating the buildstats records and
    counting matches on both sides. That formulation cannot express a shortfall
    at all - every record it counts is by construction one it matched - so the
    gate would clear on any tree, however little of the build it covered.
    """
    artifact = {
        "tasks": [
            _row("busybox", "do_compile", 0.0, 5.0),
            _row("busybox", "do_install", 0.0, 5.0),
            _row("zlib", "do_compile", 0.0, 5.0),
            _row("zlib", "do_install", 0.0, 5.0),
        ]
    }
    run = _parsed(_stat("busybox", "do_compile", 10.0), _stat("busybox", "do_install", 10.0))

    report = timing_report(artifact, baselines_path=tmp_path / "absent.json", buildstats_source=lambda: run)

    join = report.buildstats_join
    assert join.executed == 4
    assert join.joined == 2
    assert join.rate == pytest.approx(0.5)
    assert join.gate_passed is False


def test_refused_join_leaks_no_duration_into_the_rendered_section(tmp_path: Path) -> None:
    """A refusal renders no seconds anywhere - not even as a "would have been"."""
    artifact = {
        "tasks": [
            _row("busybox", "do_compile", 0.0, 5.0),
            _row("zlib", "do_compile", 0.0, 5.0),
        ]
    }
    run = _parsed(_stat("busybox", "do_compile", 1234.5))

    report = timing_report(artifact, baselines_path=tmp_path / "absent.json", buildstats_source=lambda: run)

    join = report.buildstats_join
    assert join.gate_passed is False
    assert join.cpu_seconds is None
    rendered = "\n".join(join.report_lines())
    offenders = DURATION_TOKEN.findall(rendered)
    assert offenders == [], f"refused join rendered duration tokens {offenders} in: {rendered}"
    assert "1234" not in rendered


def test_join_above_the_threshold_passes_and_carries_cpu_seconds(tmp_path: Path) -> None:
    artifact = {"tasks": [_row("busybox", "do_compile", 0.0, 5.0), _row("zlib", "do_compile", 0.0, 5.0)]}
    run = _parsed(_stat("busybox", "do_compile", 30.0), _stat("zlib", "do_compile", 12.0))

    report = timing_report(artifact, baselines_path=tmp_path / "absent.json", buildstats_source=lambda: run)

    join = report.buildstats_join
    assert join.gate_passed is True
    assert join.rate == pytest.approx(1.0)
    assert join.cpu_seconds == pytest.approx(42.0)


def test_join_ignores_buildstats_rows_this_run_never_executed(tmp_path: Path) -> None:
    """A stale capture's extra rows must not be credited to this build's CPU."""
    artifact = {"tasks": [_row("busybox", "do_compile", 0.0, 5.0)]}
    run = _parsed(_stat("busybox", "do_compile", 30.0), _stat("ghost-recipe", "do_compile", 900.0))

    report = timing_report(artifact, baselines_path=tmp_path / "absent.json", buildstats_source=lambda: run)

    assert report.buildstats_join.gate_passed is True
    assert report.buildstats_join.cpu_seconds == pytest.approx(30.0)


def test_join_matches_across_a_version_suffix_difference(tmp_path: Path) -> None:
    artifact = {"tasks": [_row("busybox-1.36.1-r0", "do_compile", 0.0, 5.0)]}
    run = _parsed(_stat("busybox-1.36.1-r1", "do_compile", 30.0))

    report = timing_report(artifact, baselines_path=tmp_path / "absent.json", buildstats_source=lambda: run)

    assert report.buildstats_join.joined == 1
    assert report.buildstats_join.gate_passed is True


def test_join_with_no_executed_tasks_refuses_rather_than_passing_vacuously(tmp_path: Path) -> None:
    """Zero over zero is a refusal: a run that measured nothing proves nothing."""
    empty_run = _parsed()

    report = timing_report({"tasks": []}, baselines_path=tmp_path / "absent.json", buildstats_source=lambda: empty_run)

    join = report.buildstats_join
    assert join.gate_passed is False
    assert join.rate == 0.0
    assert "no executed tasks" in join.note


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [("absent", "tree absent"), ("empty", "recorded nothing")],
)
def test_absent_and_empty_buildstats_trees_keep_separate_notes(tmp_path: Path, outcome: str, expected: str) -> None:
    """The three-outcome distinction from ``buildstats.read_run`` survives the join."""
    run = BuildstatsRun(outcome=outcome, note="fixture note")

    report = timing_report(
        {"tasks": [_row("busybox", "do_compile", 0.0, 5.0)]},
        baselines_path=tmp_path / "absent.json",
        buildstats_source=lambda: run,
    )

    join = report.buildstats_join
    assert join.available is False
    assert join.gate_passed is False
    assert join.cpu_seconds is None
    assert expected in join.note


def test_join_source_failure_degrades_rather_than_raising(tmp_path: Path) -> None:
    def _boom() -> BuildstatsRun:
        raise RuntimeError("tmpdir unreadable")

    report = timing_report(
        {"tasks": [_row("busybox", "do_compile", 0.0, 5.0)]},
        baselines_path=tmp_path / "absent.json",
        buildstats_source=_boom,
    )

    assert report.buildstats_join.available is False
    assert "tmpdir unreadable" in report.buildstats_join.note


def test_join_section_defaults_to_unavailable_without_a_source(tmp_path: Path) -> None:
    report = timing_report(
        {"tasks": [_row("busybox", "do_compile", 0.0, 5.0)]}, baselines_path=tmp_path / "absent.json"
    )

    assert report.buildstats_join.available is False
    assert report.buildstats_join.cpu_seconds is None


# --- CPU floor divisor ----------------------------------------------------
#
# The failure under test is that the same capture yields a different floor on
# every machine that analyses it, because the divisor was read from
# ``os.cpu_count()`` at analysis time rather than from what the build host
# recorded. It is silent by construction: both floors are arithmetically
# correct and neither says which core count produced it. So the fixtures below
# record a core count NO real host has (999), and the assertions are that the
# floor tracks the fixture and ignores the analysing host - a fixture value
# that could coincide with this machine's core count would prove nothing.

IMPOSSIBLE_CORES = 999


def _with_host(tasks: list[dict], **host: int | None) -> dict:
    return {"tasks": tasks, "host": {"cpu_count": IMPOSSIBLE_CORES, **host}}


def test_floor_divides_by_the_recorded_core_count_not_the_analysing_host(tmp_path: Path) -> None:
    artifact = _with_host([_row("busybox", "do_compile", 0.0, 5.0)])
    run = _parsed(_stat("busybox", "do_compile", 1998.0))

    report = timing_report(artifact, baselines_path=tmp_path / "absent.json", buildstats_source=lambda: run)

    floor = report.cpu_floor
    assert floor.available is True
    assert floor.divisor == IMPOSSIBLE_CORES
    assert floor.seconds == pytest.approx(1998.0 / IMPOSSIBLE_CORES)


def test_floor_is_unchanged_when_the_analysing_host_reports_a_different_core_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One capture, two analysing machines, one answer.

    Monkeypatching ``os.cpu_count`` is the direct expression of the falsifier:
    if any divisor were read at analysis time, these two floors would differ.
    """
    artifact = _with_host([_row("busybox", "do_compile", 0.0, 5.0)])
    run = _parsed(_stat("busybox", "do_compile", 1998.0))

    floors = []
    for pretend_cores in (4, 256):
        monkeypatch.setattr("os.cpu_count", lambda cores=pretend_cores: cores)
        report = timing_report(artifact, baselines_path=tmp_path / "absent.json", buildstats_source=lambda: run)
        floors.append(report.cpu_floor.seconds)

    assert floors[0] == floors[1] == pytest.approx(1998.0 / IMPOSSIBLE_CORES)


def test_rendered_floor_names_the_core_count_and_where_it_came_from(tmp_path: Path) -> None:
    """A floor nobody can attribute to a divisor cannot be checked after the fact."""
    artifact = _with_host([_row("busybox", "do_compile", 0.0, 5.0)])
    run = _parsed(_stat("busybox", "do_compile", 1998.0))

    report = timing_report(artifact, baselines_path=tmp_path / "absent.json", buildstats_source=lambda: run)

    rendered = "\n".join(report.cpu_floor.report_lines())
    assert str(IMPOSSIBLE_CORES) in rendered
    assert "recorded at capture" in rendered
    assert "build host" in rendered


def test_rendered_floor_reports_the_other_recorded_divisor_candidates(tmp_path: Path) -> None:
    """A4: if ``cpu_count`` turns out to be the wrong divisor, the alternative is captured."""
    artifact = _with_host(
        [_row("busybox", "do_compile", 0.0, 5.0)],
        bb_number_threads=12,
        parallel_make=24,
    )
    run = _parsed(_stat("busybox", "do_compile", 1998.0))

    report = timing_report(artifact, baselines_path=tmp_path / "absent.json", buildstats_source=lambda: run)

    rendered = "\n".join(report.cpu_floor.report_lines())
    assert "bb_number_threads=12" in rendered
    assert "parallel_make=24" in rendered


def test_artifact_predating_the_host_block_degrades_rather_than_falling_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No recorded count means no floor - never the analysing host's count."""
    monkeypatch.setattr("os.cpu_count", lambda: 64)
    artifact = {"tasks": [_row("busybox", "do_compile", 0.0, 5.0)]}
    run = _parsed(_stat("busybox", "do_compile", 1998.0))

    report = timing_report(artifact, baselines_path=tmp_path / "absent.json", buildstats_source=lambda: run)

    floor = report.cpu_floor
    assert floor.available is False
    assert floor.seconds is None
    assert floor.divisor is None
    assert "records no build-host core count" in floor.note
    assert "64" not in floor.note
    assert str(1998.0 / 64) not in floor.note


@pytest.mark.parametrize("cpu_count", [None, 0, -4, "16", True])
def test_unusable_recorded_core_counts_degrade(tmp_path: Path, cpu_count: object) -> None:
    """A zero, a negative, a string or a bool is "not recorded", not a divisor."""
    artifact = {"tasks": [_row("busybox", "do_compile", 0.0, 5.0)], "host": {"cpu_count": cpu_count}}
    run = _parsed(_stat("busybox", "do_compile", 1998.0))

    report = timing_report(artifact, baselines_path=tmp_path / "absent.json", buildstats_source=lambda: run)

    assert report.cpu_floor.available is False
    assert report.cpu_floor.seconds is None


def test_refused_join_leaves_the_floor_unavailable_and_renders_no_duration(tmp_path: Path) -> None:
    """The gate from task 1.3 is the only input; there is nothing else to reach for."""
    artifact = _with_host(
        [
            _row("busybox", "do_compile", 0.0, 5.0),
            _row("zlib", "do_compile", 0.0, 5.0),
        ]
    )
    run = _parsed(_stat("busybox", "do_compile", 1234.5))

    report = timing_report(artifact, baselines_path=tmp_path / "absent.json", buildstats_source=lambda: run)

    floor = report.cpu_floor
    assert report.buildstats_join.gate_passed is False
    assert floor.available is False
    assert floor.seconds is None
    rendered = "\n".join(floor.report_lines())
    offenders = DURATION_TOKEN.findall(rendered)
    assert offenders == [], f"refused floor rendered duration tokens {offenders} in: {rendered}"
    assert "1234" not in rendered


def test_floor_section_defaults_to_unavailable_without_a_buildstats_source(tmp_path: Path) -> None:
    report = timing_report(
        _with_host([_row("busybox", "do_compile", 0.0, 5.0)]), baselines_path=tmp_path / "absent.json"
    )

    assert report.cpu_floor.available is False
    assert report.cpu_floor.seconds is None


def test_normalize_records_the_build_hosts_divisor_candidates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The capture side of the same contract: the count is written where the build ran.

    Kept beside the analysis tests deliberately - the property under test spans
    both halves, and splitting it leaves each file asserting something true of
    itself and nothing about the pair.
    """
    monkeypatch.setattr("os.cpu_count", lambda: 48)
    monkeypatch.setenv("BAKAR_BB_NUMBER_THREADS", "12")
    monkeypatch.setenv("BAKAR_PARALLEL_MAKE", "-j 24")

    artifact = eventlog.normalize(tmp_path / "no-such-event.log")

    assert artifact["host"] == {"cpu_count": 48, "bb_number_threads": 12, "parallel_make": 24}


def test_normalize_prefers_the_builds_own_variable_dump_over_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dump is what bitbake ran with; the host env is only a fallback.

    ``steps.kas_build._build_env`` hands the BAKAR_* pair to the kas subprocess
    rather than exporting it here, so reading the environment alone would record
    ``None`` on every real build and leave A4's alternative divisor uncaptured.
    """
    monkeypatch.setenv("BAKAR_BB_NUMBER_THREADS", "99")
    monkeypatch.setenv("BAKAR_PARALLEL_MAKE", "-j 99")
    log = tmp_path / "bitbake_eventlog.json"
    log.write_text(
        json.dumps({"allvariables": {"BB_NUMBER_THREADS": "8", "PARALLEL_MAKE": "-j 16"}}) + "\n",
        encoding="utf-8",
    )

    artifact = eventlog.normalize(log)

    assert artifact["host"]["bb_number_threads"] == 8
    assert artifact["host"]["parallel_make"] == 16


def test_normalize_records_no_parallelism_when_the_environment_carries_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unset or unparseable knob is ``None``, never a default that reads as measured."""
    for name in ("BAKAR_BB_NUMBER_THREADS", "BB_NUMBER_THREADS", "BAKAR_PARALLEL_MAKE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PARALLEL_MAKE", "-j auto")

    artifact = eventlog.normalize(tmp_path / "no-such-event.log")

    assert artifact["host"]["bb_number_threads"] is None
    assert artifact["host"]["parallel_make"] is None


# --- concurrency floor: max(CPU floor, critical path) ---------------------
#
# The number alone is not the deliverable. A dependency-bound build and a
# throughput-bound one want opposite responses, and the max on its own cannot
# tell them apart - which is how a CPU-only 11.9% headroom read as actionable
# on a build whose real headroom was 1.1%. Both scenarios are covered below
# with the reference figures, so a regression that drops the binding bound
# fails on the very case that motivated the section.


def _floor_artifact(a_seconds: float, b_seconds: float, actual: float, cpu_count: int = 4) -> dict:
    """Two chained recipes (``a -> b``) with a recorded core count and build span."""
    return {
        "tasks": [
            _row("a", "do_compile", 0.0, a_seconds),
            _row("b", "do_compile", a_seconds, a_seconds + b_seconds),
        ],
        "host": {"cpu_count": cpu_count},
        "build": {"started": 0.0, "completed": actual},
    }


def _chain_source() -> tuple[str, str]:
    return 'digraph { "a.do_compile" -> "b.do_compile"; }', ""


def test_dependency_bound_build_names_the_critical_path_as_the_binding_bound(tmp_path: Path) -> None:
    """The reference case: CPU floor 23.0 min, path 25.8 min, build 26.1 min.

    Read off the CPU floor alone the headroom is 11.9% and looks worth a
    scheduling campaign. Against the bound that actually binds it is 1.1%. The
    assertion that the rendered text NAMES the path as binding is the whole
    point - a report that printed only ``max`` would be identical here and in
    the throughput-bound case below.
    """
    artifact = _floor_artifact(a_seconds=548.0, b_seconds=1000.0, actual=1566.0, cpu_count=4)
    run = _parsed(_stat("a", "do_compile", 2520.0), _stat("b", "do_compile", 3000.0))

    report = timing_report(
        artifact,
        baselines_path=tmp_path / "absent.json",
        dependency_source=_chain_source,
        buildstats_source=lambda: run,
    )

    floor = report.concurrency_floor
    assert floor.available is True
    assert floor.binding == "path"
    assert floor.cpu_seconds == pytest.approx(1380.0)
    assert floor.path_seconds == pytest.approx(1548.0)
    assert floor.seconds == pytest.approx(1548.0)
    assert floor.headroom_seconds == pytest.approx(18.0)
    assert floor.headroom_pct == pytest.approx(1.149, abs=0.01)

    rendered = "\n".join(floor.report_lines())
    assert "the critical path binds" in rendered
    assert "1.1%" in rendered
    # The misleading CPU-only figure must not appear anywhere in this section.
    assert "11.9" not in rendered


def test_throughput_bound_build_names_cpu_capacity_as_the_binding_bound(tmp_path: Path) -> None:
    artifact = _floor_artifact(a_seconds=100.0, b_seconds=200.0, actual=1200.0, cpu_count=4)
    run = _parsed(_stat("a", "do_compile", 1500.0), _stat("b", "do_compile", 2500.0))

    report = timing_report(
        artifact,
        baselines_path=tmp_path / "absent.json",
        dependency_source=_chain_source,
        buildstats_source=lambda: run,
    )

    floor = report.concurrency_floor
    assert floor.available is True
    assert floor.binding == "cpu"
    assert floor.seconds == pytest.approx(1000.0)
    assert floor.path_seconds == pytest.approx(300.0)
    assert floor.headroom_seconds == pytest.approx(200.0)

    rendered = "\n".join(floor.report_lines())
    assert "CPU capacity binds" in rendered
    assert "16.7%" in rendered


def test_floor_states_the_basis_of_each_input_and_computes_the_headroom_itself(tmp_path: Path) -> None:
    """Design D3's mitigation: the two bounds are measured differently, and the
    output says so rather than leaving a reader to subtract them."""
    artifact = _floor_artifact(a_seconds=548.0, b_seconds=1000.0, actual=1566.0)
    run = _parsed(_stat("a", "do_compile", 2520.0), _stat("b", "do_compile", 3000.0))

    report = timing_report(
        artifact,
        baselines_path=tmp_path / "absent.json",
        dependency_source=_chain_source,
        buildstats_source=lambda: run,
    )

    rendered = "\n".join(report.concurrency_floor.report_lines())
    assert "recipe-level" in rendered
    assert "task-level" in rendered
    assert "not converted" in rendered
    # The headroom figure is produced by the tool, not implied.
    assert "headroom" in rendered
    assert report.concurrency_floor.headroom_seconds is not None


def test_floor_unavailable_when_the_critical_path_is_unavailable(tmp_path: Path) -> None:
    """A CPU floor alone is not a concurrency floor, and must not render as one."""
    artifact = _floor_artifact(a_seconds=548.0, b_seconds=1000.0, actual=1566.0)
    run = _parsed(_stat("a", "do_compile", 2520.0), _stat("b", "do_compile", 3000.0))

    report = timing_report(
        artifact,
        baselines_path=tmp_path / "absent.json",
        buildstats_source=lambda: run,
    )

    floor = report.concurrency_floor
    assert report.cpu_floor.available is True
    assert floor.available is False
    assert floor.binding is None
    rendered = "\n".join(floor.report_lines())
    offenders = DURATION_TOKEN.findall(rendered)
    assert offenders == [], f"unavailable floor rendered duration tokens {offenders} in: {rendered}"
    assert "critical path is unavailable" in rendered


def test_floor_unavailable_when_the_cpu_floor_is_refused(tmp_path: Path) -> None:
    """A path alone is not a concurrency floor either - the refusal is symmetric."""
    artifact = _floor_artifact(a_seconds=548.0, b_seconds=1000.0, actual=1566.0)
    # Only one of the two executed tasks carries a record: 50%, below the gate.
    run = _parsed(_stat("a", "do_compile", 2520.0))

    report = timing_report(
        artifact,
        baselines_path=tmp_path / "absent.json",
        dependency_source=_chain_source,
        buildstats_source=lambda: run,
    )

    assert report.critical_path.available is True
    floor = report.concurrency_floor
    assert floor.available is False
    rendered = "\n".join(floor.report_lines())
    offenders = DURATION_TOKEN.findall(rendered)
    assert offenders == [], f"unavailable floor rendered duration tokens {offenders} in: {rendered}"
    assert "CPU floor is unavailable" in rendered


def test_headroom_unavailable_when_the_artifact_records_no_build_span(tmp_path: Path) -> None:
    """The floor and its binding bound still render; the headroom says it cannot."""
    artifact = _floor_artifact(a_seconds=548.0, b_seconds=1000.0, actual=1566.0)
    del artifact["build"]
    run = _parsed(_stat("a", "do_compile", 2520.0), _stat("b", "do_compile", 3000.0))

    report = timing_report(
        artifact,
        baselines_path=tmp_path / "absent.json",
        dependency_source=_chain_source,
        buildstats_source=lambda: run,
    )

    floor = report.concurrency_floor
    assert floor.available is True
    assert floor.binding == "path"
    assert floor.actual_seconds is None
    assert floor.headroom_seconds is None
    assert floor.headroom_pct is None
    rendered = "\n".join(floor.report_lines())
    assert "headroom unavailable" in rendered
    assert "%" not in rendered.split("headroom unavailable")[1]


def test_negative_headroom_reads_as_an_over_weighted_path_not_a_beaten_floor(tmp_path: Path) -> None:
    """The recipe-level path over-weights by construction (D3), so it can exceed
    the real build. That must not render as though the build outran its floor."""
    artifact = _floor_artifact(a_seconds=548.0, b_seconds=1000.0, actual=1200.0)
    run = _parsed(_stat("a", "do_compile", 2520.0), _stat("b", "do_compile", 3000.0))

    report = timing_report(
        artifact,
        baselines_path=tmp_path / "absent.json",
        dependency_source=_chain_source,
        buildstats_source=lambda: run,
    )

    floor = report.concurrency_floor
    assert floor.available is True
    assert floor.headroom_seconds == pytest.approx(-348.0)
    rendered = "\n".join(floor.report_lines())
    assert "over-estimate" in rendered
    assert "headroom negative" in rendered


def test_floor_section_defaults_to_unavailable_without_either_source(tmp_path: Path) -> None:
    report = timing_report({"tasks": []}, baselines_path=tmp_path / "absent.json")

    assert report.concurrency_floor.available is False
    assert report.concurrency_floor.seconds is None


# --- churn columns --------------------------------------------------------
#
# These tests go through a REAL buildstats tree on disk rather than through
# hand-built ``TaskStats`` objects. The failure they exist to catch is a
# mistyped field name - the file spells the counters ``rusage ru_minflt`` and
# ``Child rusage ru_majflt``, not ``minflt``/``ru_minflt`` - and a fixture that
# constructs ``TaskStats`` directly bypasses the parser that could get it wrong,
# so every column would read as populated over a spelling nothing checks.

#: One realistic per-task record. The self/child split is deliberately lopsided
#: the way a real one is: on a measured capture ``do_configure`` read 7,023,117
#: self minor faults against 214,243,919 child ones, so a reader that sums only
#: the self lines under-reports by roughly 30x and still renders a full table.
_TASK_FILE = """Event: TaskStarted
Recipe: {recipe}
Task: {task}
Started: 1000.0
Elapsed time: {elapsed} seconds
utime: 16
stime: 4
cutime: 900
cstime: 200
rusage ru_utime: 1.5
rusage ru_stime: 0.5
rusage ru_minflt: {minflt}
rusage ru_majflt: {majflt}
Child rusage ru_utime: 9.0
Child rusage ru_stime: 1.0
Child rusage ru_minflt: {cminflt}
Child rusage ru_majflt: {cmajflt}
IO syscr: {syscr}
IO syscw: {syscw}
IO write_bytes: {write_bytes}
Status: PASSED
"""


def _write_capture(tmpdir: Path, records: list[dict]) -> None:
    """Write a buildstats tree in bitbake's own on-disk shape."""
    capture = tmpdir / "buildstats" / "20260101000000"
    for rec in records:
        recipe_dir = capture / rec["recipe"]
        recipe_dir.mkdir(parents=True, exist_ok=True)
        (recipe_dir / rec["task"]).write_text(_TASK_FILE.format(**rec))


def _churn_record(recipe: str, task: str, **over: object) -> dict:
    base = {
        "recipe": recipe,
        "task": task,
        "elapsed": 12.0,
        "minflt": 7_023_117,
        "majflt": 40,
        "cminflt": 214_243_919,
        "cmajflt": 60,
        "syscr": 1_000,
        "syscw": 500,
        "write_bytes": 2_000_000_000,
    }
    base.update(over)
    return base


def _churn_report(tmp_path: Path, records: list[dict], rows: list[dict]) -> TimingReport:
    from bakar import buildstats

    _write_capture(tmp_path, records)
    return timing_report(
        {"tasks": rows},
        baselines_path=tmp_path / "absent.json",
        buildstats_source=lambda: buildstats.read_run(tmp_path),
    )


def test_churn_counters_are_non_zero_and_read_by_field_name(tmp_path: Path) -> None:
    """The falsifier for this capability, asserted directly.

    A mistyped field name plus a ``.get(field, 0)`` fallback renders a table of
    zeros that looks populated. So the assertion is on the VALUES, not on the
    keys being present: each column must carry the self+child sum the file
    actually spells out.
    """
    report = _churn_report(
        tmp_path,
        [_churn_record("busybox", "do_configure")],
        [_row("busybox", "do_configure", 0.0, 12.0)],
    )

    churn = report.task_churn
    assert churn.available is True
    (row,) = churn.rows
    assert row.task == "do_configure"
    assert row.minflt == 7_023_117 + 214_243_919
    assert row.majflt == 40 + 60
    assert row.syscalls == 1_000 + 500
    assert row.write_bytes == 2_000_000_000
    assert row.minflt > 0 and row.majflt > 0 and row.syscalls > 0


def test_churn_sums_across_recipes_within_one_task_type(tmp_path: Path) -> None:
    report = _churn_report(
        tmp_path,
        [_churn_record("busybox", "do_compile"), _churn_record("zlib", "do_compile")],
        [_row("busybox", "do_compile", 0.0, 12.0), _row("zlib", "do_compile", 0.0, 12.0)],
    )

    (row,) = report.task_churn.rows
    assert row.tasks == 2
    assert row.minflt == 2 * (7_023_117 + 214_243_919)
    assert report.task_churn.covered == 2
    assert report.task_churn.executed == 2


def test_churn_ignores_records_this_run_never_executed(tmp_path: Path) -> None:
    """A stale capture's rows describe a different build and must not be counted."""
    report = _churn_report(
        tmp_path,
        [_churn_record("busybox", "do_compile"), _churn_record("ghost-recipe", "do_compile")],
        [_row("busybox", "do_compile", 0.0, 12.0)],
    )

    (row,) = report.task_churn.rows
    assert row.tasks == 1
    assert row.minflt == 7_023_117 + 214_243_919
    assert report.task_churn.covered == 1


def test_churn_row_values_sit_under_the_columns_that_name_them(tmp_path: Path) -> None:
    """Read the table back by column name, which is how the reading error happened.

    An earlier reading of the reference analyser's own output took the ``majflt``
    column for a task count and ``GB_wr`` for cores-per-task. Slicing the header
    and the row on the same widths is the check that a value ends up under its
    own name.
    """
    from bakar.insights_timing import CHURN_COLUMNS, CHURN_WIDTHS

    report = _churn_report(
        tmp_path,
        [_churn_record("busybox", "do_configure")],
        [_row("busybox", "do_configure", 0.0, 12.0)],
    )
    lines = report.task_churn.report_lines()
    header, row_line = lines[-2], lines[-1]

    def _cells(line: str) -> dict[str, str]:
        cells, pos = {}, 1
        for name, width in zip(CHURN_COLUMNS, CHURN_WIDTHS, strict=True):
            cells[name] = line[pos : pos + width].strip()
            pos += width
        return cells

    assert list(_cells(header).values()) == list(CHURN_COLUMNS)
    cells = _cells(row_line)
    assert cells["task type"] == "do_configure"
    assert cells["tasks"] == "1"
    assert cells["minflt"] == f"{7_023_117 + 214_243_919:,}"
    assert cells["majflt"] == "100"
    assert cells["syscalls"] == "1,500"
    assert cells["GB_wr"] == "2.00"


def test_churn_is_reported_even_when_the_join_gate_refuses(tmp_path: Path) -> None:
    """A per-task-type aggregate survives partial coverage; a build-wide bound does not.

    The floor stays refused in the same report, so this is not a hole in the
    gate - it is the gate applying to the figure it was written for.
    """
    report = _churn_report(
        tmp_path,
        [_churn_record("busybox", "do_compile")],
        [_row("busybox", "do_compile", 0.0, 12.0), _row("zlib", "do_compile", 0.0, 12.0)],
    )

    assert report.buildstats_join.gate_passed is False
    assert report.cpu_floor.available is False
    churn = report.task_churn
    assert churn.available is True
    assert churn.covered == 1
    assert churn.executed == 2
    assert "1 of 2 executed tasks" in churn.note


def test_churn_rows_are_ordered_by_minor_faults(tmp_path: Path) -> None:
    report = _churn_report(
        tmp_path,
        [
            _churn_record("busybox", "do_unpack", minflt=100, cminflt=200),
            _churn_record("busybox", "do_configure"),
        ],
        [_row("busybox", "do_unpack", 0.0, 12.0), _row("busybox", "do_configure", 0.0, 12.0)],
    )

    assert [r.task for r in report.task_churn.rows] == ["do_configure", "do_unpack"]


def test_churn_with_no_matching_records_is_unavailable(tmp_path: Path) -> None:
    report = _churn_report(
        tmp_path,
        [_churn_record("ghost-recipe", "do_compile")],
        [_row("busybox", "do_compile", 0.0, 12.0)],
    )

    churn = report.task_churn
    assert churn.available is False
    assert churn.rows == []
    assert "match an executed task" in churn.note


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [("absent", "tree absent"), ("empty", "recorded nothing")],
)
def test_churn_keeps_absent_and_empty_trees_apart(tmp_path: Path, outcome: str, expected: str) -> None:
    run = BuildstatsRun(outcome=outcome, note="fixture note")

    report = timing_report(
        {"tasks": [_row("busybox", "do_compile", 0.0, 5.0)]},
        baselines_path=tmp_path / "absent.json",
        buildstats_source=lambda: run,
    )

    assert report.task_churn.available is False
    assert expected in report.task_churn.note


def test_churn_source_failure_degrades_rather_than_raising(tmp_path: Path) -> None:
    def _boom() -> BuildstatsRun:
        raise RuntimeError("tmpdir unreadable")

    report = timing_report(
        {"tasks": [_row("busybox", "do_compile", 0.0, 5.0)]},
        baselines_path=tmp_path / "absent.json",
        buildstats_source=_boom,
    )

    assert report.task_churn.available is False
    assert "source failed" in report.task_churn.note


def test_churn_defaults_to_unavailable_without_a_buildstats_source(tmp_path: Path) -> None:
    report = timing_report({"tasks": []}, baselines_path=tmp_path / "absent.json")

    assert report.task_churn.available is False
    assert report.task_churn.rows == []


def test_churn_coverage_counts_distinct_tasks_not_records(tmp_path: Path) -> None:
    """Two versioned recipe directories collapse to one key once the version is stripped.

    Counting records instead would report coverage of 2 over 1 executed task -
    a coverage figure above 100%, which reads as a parsing bug rather than as
    the aggregation it actually is.
    """
    report = _churn_report(
        tmp_path,
        [_churn_record("busybox-1.36.1-r0", "do_compile"), _churn_record("busybox-1.37-r0", "do_compile")],
        [_row("busybox-1.36.1-r0", "do_compile", 0.0, 12.0)],
    )

    churn = report.task_churn
    assert churn.covered == 1
    assert churn.executed == 1
    (row,) = churn.rows
    assert row.tasks == 2


# --- 3.2: the columns rank task types across a wide dynamic range ------------
#
# The numbers below are MEASURED. They were read off a real capture - imx93-frdm
# run 20260909142035, 5808 per-task buildstats files - by summing each task
# type's self and child rusage counters, and they exist independently of this
# module. A fixture invented to satisfy the assertion would agree with whatever
# the aggregation happens to do and prove only that division works; these agree
# with a build that actually ran.
#
# The criterion is the RANKING property, not a named pair. An earlier form of
# this task asserted that ``do_configure`` and ``do_unpack`` differ by two
# orders of magnitude of minflt/majflt. Measurement falsified it on every
# capture available: the best separation was 0.99 orders, the sign inverted on
# two of three captures, and the spread moved from -0.99 to +0.74 orders across
# them. Any named pair is a property of the recipe mix, so it rots. What held
# on every capture is that the columns spread the task types WIDELY - 3.68
# orders across the 21 types on the richest one.

#: ``(task type, minflt, majflt)`` per task type, self+child summed, as the
#: capture recorded them. Ordered by minflt/majflt descending, which is the
#: ranking the test asserts the aggregation reproduces.
_MEASURED_CHURN = (
    ("do_deploy_source_date_epoch", 5_642_875, 12),
    ("do_recipe_qa", 3_346_334, 15),
    ("do_create_recipe_spdx", 4_550_252, 36),
    ("do_unpack", 10_359_932, 509),
    ("do_configure", 221_267_036, 106_004),
    ("do_compile", 667_163_720, 1_717_566),
    ("do_install", 77_726_837, 311_751),
    ("do_create_spdx", 5_638_072, 48_407),
    ("do_configure_ptest_base", 665_308, 6_764),
)

#: The spread the capture exhibits, in orders of magnitude of minflt/majflt.
_MEASURED_SPREAD_ORDERS = 3.68


def _split_self_child(total: int) -> tuple[int, int]:
    """Split a measured total across the self and child rusage lines.

    A real task file carries the bulk of its churn on the child lines - the work
    happens in spawned processes - so the fixture has to as well, or it would
    exercise a summation the capture never had. The split preserves the total
    exactly, which is the part the assertions depend on.
    """
    own = total // 32
    return own, total - own


def _measured_capture_report(tmp_path: Path) -> TimingReport:
    """Write the measured per-type totals out as a real buildstats tree."""
    records, rows = [], []
    for task, minflt, majflt in _MEASURED_CHURN:
        recipe = f"agg-{task}"
        own_min, child_min = _split_self_child(minflt)
        own_maj, child_maj = _split_self_child(majflt)
        records.append(
            _churn_record(
                recipe,
                task,
                minflt=own_min,
                cminflt=child_min,
                majflt=own_maj,
                cmajflt=child_maj,
            )
        )
        rows.append(_row(recipe, task, 0.0, 12.0))
    return _churn_report(tmp_path, records, rows)


def _measured_ratios(report: TimingReport) -> dict[str, float]:
    return {row.task: row.minflt / row.majflt for row in report.task_churn.rows}


def test_churn_columns_spread_task_types_over_orders_of_magnitude(tmp_path: Path) -> None:
    """The amended criterion: the columns discriminate, they do not bunch.

    Two orders is the bar. A capability whose every task type lands within one
    order cannot tell a process-churn profile from an I/O one, which is the only
    thing these columns are for.
    """
    report = _measured_capture_report(tmp_path)

    ratios = _measured_ratios(report)
    assert len(ratios) == len(_MEASURED_CHURN)

    spread = math.log10(max(ratios.values()) / min(ratios.values()))
    assert spread >= 2.0
    assert spread == pytest.approx(_MEASURED_SPREAD_ORDERS, abs=0.01)


def test_churn_columns_rank_task_types_in_the_measured_order(tmp_path: Path) -> None:
    """The aggregation reproduces the ranking the capture exhibits.

    The spread assertion alone would pass on a table whose values landed under
    the wrong task types, so the order is asserted separately against the
    measured one.
    """
    report = _measured_capture_report(tmp_path)

    ratios = _measured_ratios(report)
    ranked = sorted(ratios, key=lambda task: ratios[task], reverse=True)

    assert ranked == [task for task, _, _ in _MEASURED_CHURN]


# --- A5: the pre-existing sections are unperturbed by the buildstats source ---
#
# A5 claims a DIFFERENCE between two versions of the code, so an expectation
# derived from the current implementation would prove nothing. The values below
# were produced by running the PRE-change ``insights_timing.timing_report``
# (``git show a7ed1dd:src/bakar/insights_timing.py``, the commit immediately
# before the buildstats reader landed) over ``_A5_ARTIFACT`` and ``_A5_DOT``,
# and are recorded as literals so they stay meaningful once that commit is no
# longer close to HEAD. If one of these assertions fails, the falsifier for
# task 2.3 has fired - the new source perturbed a section it was not supposed
# to touch. Do not edit the literals to agree with current output.
#
# The artifact's ``host`` block and ``eventlog.SCHEMA_VERSION`` bump are NOT
# covered here: A5 is about these two report sections for a given input, not
# about the artifact's shape.

_A5_ARTIFACT = {
    "tasks": [
        _row("busybox", "do_compile", 100.0, 242.5),
        _row("busybox", "do_configure", 40.0, 100.0),
        _row("busybox", "do_unpack", 10.0, 40.0),
        _row("zlib", "do_compile", 20.0, 55.0),
        _row("zlib", "do_configure", 5.0, 20.0),
        _row("openssl", "do_compile", 250.0, 520.0),
        _row("openssl", "do_configure", 200.0, 250.0),
        _row("gcc", "do_compile", 0.0, 900.0),
        _row("gcc", "do_install", 900.0, 960.0),
        _row("make", "do_compile", 60.0, 78.0),
        _row("make", "do_configure", 50.0, 60.0),
        _row("bash", "do_compile", 70.0, 95.0),
        _row("bash", "do_configure", 65.0, 70.0),
    ],
    "started": 0.0,
    "completed": 1000.0,
}

_A5_DOT = (
    "digraph { "
    '"zlib.do_configure" -> "zlib.do_compile"; '
    '"zlib.do_compile" -> "busybox.do_configure"; '
    '"busybox.do_unpack" -> "busybox.do_configure"; '
    '"busybox.do_configure" -> "busybox.do_compile"; '
    '"busybox.do_compile" -> "openssl.do_configure"; '
    '"openssl.do_configure" -> "openssl.do_compile"; '
    '"make.do_configure" -> "make.do_compile"; '
    '"make.do_compile" -> "bash.do_compile"; '
    "}"
)

# Reference output of the pre-change code. Thirteen tasks, so top-N truncates
# at ten; the two 60.0s rows keep their artifact order under the stable sort.
_A5_TOP_SLOWEST = [
    ("gcc", "do_compile", 900.0),
    ("openssl", "do_compile", 270.0),
    ("busybox", "do_compile", 142.5),
    ("busybox", "do_configure", 60.0),
    ("gcc", "do_install", 60.0),
    ("openssl", "do_configure", 50.0),
    ("zlib", "do_compile", 35.0),
    ("busybox", "do_unpack", 30.0),
    ("bash", "do_compile", 25.0),
    ("make", "do_compile", 18.0),
]
_A5_PATH_CHAIN = ["zlib", "busybox", "openssl"]
_A5_PATH_SECONDS = 602.5


def _a5_dependency_source() -> tuple[str, str]:
    return _A5_DOT, ""


def _assert_a5_sections_unchanged(report: TimingReport, *, path_available: bool) -> None:
    """Assert both pre-existing sections match the pre-change reference."""
    assert [(d.recipe, d.task, d.duration) for d in report.top_slowest] == _A5_TOP_SLOWEST
    assert all(d.baseline_mean is None and d.baseline_stddev is None for d in report.top_slowest)

    path = report.critical_path
    assert path.available is path_available
    if path_available:
        assert path.chain == _A5_PATH_CHAIN
        assert path.total_seconds == pytest.approx(_A5_PATH_SECONDS)
    else:
        assert path.chain == []
        assert path.total_seconds == pytest.approx(0.0)


@pytest.mark.unit
def test_a5_sections_match_the_pre_change_reference_without_any_buildstats_source(tmp_path: Path) -> None:
    """No buildstats source supplied at all - the baseline case for A5."""
    report = timing_report(_A5_ARTIFACT, baselines_path=tmp_path / "absent.json")
    _assert_a5_sections_unchanged(report, path_available=False)

    report = timing_report(
        _A5_ARTIFACT,
        baselines_path=tmp_path / "absent.json",
        dependency_source=_a5_dependency_source,
    )
    _assert_a5_sections_unchanged(report, path_available=True)


@pytest.mark.unit
@pytest.mark.parametrize("outcome", ["absent", "empty"])
def test_a5_sections_survive_a_buildstats_tree_that_cannot_be_read(tmp_path: Path, outcome: str) -> None:
    """A source that is present but yields nothing is where a perturbation hides.

    The new sections must degrade; the two old ones must be identical to the
    pre-change reference regardless.
    """
    run = BuildstatsRun(outcome=outcome, note="fixture note")

    report = timing_report(
        _A5_ARTIFACT,
        baselines_path=tmp_path / "absent.json",
        dependency_source=_a5_dependency_source,
        buildstats_source=lambda: run,
    )

    assert report.buildstats_join.available is False
    assert report.concurrency_floor.available is False
    _assert_a5_sections_unchanged(report, path_available=True)


@pytest.mark.unit
def test_a5_sections_survive_a_buildstats_source_that_raises(tmp_path: Path) -> None:
    def _boom() -> BuildstatsRun:
        raise RuntimeError("buildstats tree unreadable")

    report = timing_report(
        _A5_ARTIFACT,
        baselines_path=tmp_path / "absent.json",
        dependency_source=_a5_dependency_source,
        buildstats_source=_boom,
    )

    assert report.buildstats_join.available is False
    _assert_a5_sections_unchanged(report, path_available=True)


@pytest.mark.unit
def test_a5_sections_survive_a_buildstats_tree_that_refuses_the_join(tmp_path: Path) -> None:
    """A tree that parses but covers one task of thirteen: the gate refuses.

    This is the case a perturbation would hide in - the new code runs its full
    join and floor path over real records and still must leave the two old
    sections byte-identical.
    """
    run = _parsed(_stat("busybox", "do_compile", 120.0))

    report = timing_report(
        _A5_ARTIFACT,
        baselines_path=tmp_path / "absent.json",
        dependency_source=_a5_dependency_source,
        buildstats_source=lambda: run,
    )

    assert report.buildstats_join.gate_passed is False
    assert report.concurrency_floor.available is False
    _assert_a5_sections_unchanged(report, path_available=True)
