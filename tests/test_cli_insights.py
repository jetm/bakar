"""Integration test for the ``bakar insights`` command.

Drives the command through the Typer ``CliRunner`` against a real fixture run
directory - a synthetic ``bitbake_eventlog.json`` (pickled events, wire format
per ``tests/test_eventlog.py``) plus persisted ``psi-samples.json`` and
``disk-samples.json`` sibling files - so the four analysis modules
(:mod:`bakar.insights_sstate`, :mod:`bakar.insights_timing`,
:mod:`bakar.insights_pressure`, :mod:`bakar.insights_disk`) run for real
against fixture data rather than being mocked. Unlike ``tests/test_cli_report.py``
this test does not monkeypatch ``_find_run``: the fixture run directory is laid
out under the ``nxp_workspace`` fixture's real search path so the command's own
run-resolution logic locates it.
"""

from __future__ import annotations

import base64
import json
import pickle
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

import bakar.commands.insights as insights_module  # noqa: F401  (registers the command on import)
from bakar import eventlog
from bakar.cli import app

if TYPE_CHECKING:
    from pathlib import Path

    from typer.testing import CliRunner as _CliRunner

pytestmark = pytest.mark.unit

RUN_ID = "20260527-100000"


class _StubEvent:
    """Stand-in for a pickled bitbake event object (mirrors test_eventlog.py)."""


def _encode_event(**attrs: object) -> str:
    """base64(pickle(obj)) - the wire format of an event log ``vars`` payload."""
    obj = _StubEvent()
    for key, value in attrs.items():
        setattr(obj, key, value)
    # nosemgrep: python.lang.security.deserialization.pickle.avoid-pickle
    return base64.b64encode(pickle.dumps(obj)).decode("ascii")


def _line(class_name: str, **attrs: object) -> str:
    return json.dumps({"class": class_name, "vars": _encode_event(**attrs)})


def _build_eventlog() -> str:
    """A synthetic event log exercising sstate, timing, and disk-full sections."""
    lines = [
        _line("bb.event.BuildStarted", time=1000.0, name="build", pkgs=["core-image-minimal"]),
        # Timing: two do_compile tasks with distinct durations for top-N ranking.
        _line(
            "bb.build.TaskStarted",
            _task="do_compile",
            _package="busybox-1.36.1-r0",
            taskname="do_compile",
            logfile="/work/build/tmp/work/cortexa53/busybox/1.36.1-r0/temp/log.do_compile.4242",
            pid=4242,
            time=1000.0,
        ),
        _line(
            "bb.build.TaskSucceeded",
            _task="do_compile",
            _package="busybox-1.36.1-r0",
            taskname="do_compile",
            time=1040.0,
        ),
        _line(
            "bb.build.TaskStarted",
            _task="do_compile",
            _package="coreutils-9.4-r0",
            taskname="do_compile",
            logfile="/work/build/tmp/work/cortexa53/coreutils/9.4-r0/temp/log.do_compile.4300",
            pid=4300,
            time=1000.0,
        ),
        _line(
            "bb.build.TaskSucceeded",
            _task="do_compile",
            _package="coreutils-9.4-r0",
            taskname="do_compile",
            time=1010.0,
        ),
        # sstate: one setscene hit (busybox), one setscene miss (zlib).
        _line(
            "bb.build.TaskStarted",
            _task="do_populate_sysroot_setscene",
            _package="busybox-1.36.1-r0",
            taskname="do_populate_sysroot_setscene",
            time=999.0,
        ),
        _line(
            "bb.build.TaskSucceeded",
            _task="do_populate_sysroot_setscene",
            _package="busybox-1.36.1-r0",
            taskname="do_populate_sysroot_setscene",
            time=999.5,
        ),
        _line(
            "bb.build.TaskFailedSilent",
            _task="do_fetch_setscene",
            _package="zlib-1.3-r0",
            taskname="do_fetch_setscene",
            logfile="/work/build/tmp/work/cortexa53/zlib/1.3-r0/temp/log.do_fetch_setscene.6262",
            time=999.2,
        ),
        # disk: a DiskFull event, surfaced independent of the growth figure.
        # Real bb.event.DiskFull.__init__(dev, type, freespace, mountpoint)
        # sets _dev/_type/_free/_mountpoint - no time/path/message attribute.
        _line(
            "bb.event.DiskFull",
            _dev="/dev/sda1",
            _type="ext4",
            _free=1024,
            _mountpoint="/work/build",
        ),
        _line("bb.event.BuildCompleted", time=1040.0),
    ]
    return "\n".join(lines) + "\n"


@pytest.fixture
def insights_run_dir(nxp_workspace: Path) -> Path:
    """A real ``nxp/build/runs/<run_id>`` dir with eventlog + PSI/disk samples."""
    run_dir = nxp_workspace / "nxp" / "build" / "runs" / RUN_ID
    run_dir.mkdir(parents=True)
    (run_dir / "bitbake_eventlog.json").write_text(_build_eventlog(), encoding="utf-8")
    (run_dir / "psi-samples.json").write_text(
        json.dumps(
            [
                {"time": 1000.0, "cpu": 15.0, "io": 5.0, "memory": 2.0},
                {"time": 1020.0, "cpu": 20.0, "io": 6.0, "memory": 3.0},
            ]
        ),
        encoding="utf-8",
    )
    (run_dir / "disk-samples.json").write_text(
        json.dumps(
            [
                {"time": 1000.0, "used_bytes": 1_000_000},
                {"time": 1040.0, "used_bytes": 1_500_000},
            ]
        ),
        encoding="utf-8",
    )
    return run_dir


@pytest.mark.unit
def test_insights_default_renders_all_four_sections(
    runner: _CliRunner, nxp_workspace: Path, insights_run_dir: Path
) -> None:
    """With no selector flags, all four sections render against real fixture data."""
    result = runner.invoke(app, ["insights", "--workspace", str(nxp_workspace)])
    assert result.exit_code == 0, result.output

    # sstate: busybox setscene hit and zlib setscene miss.
    assert "sstate:" in result.output
    assert "busybox-1.36.1-r0" in result.output
    assert "zlib-1.3-r0" in result.output

    # timing: the slower do_compile task (busybox, 40s) shows up.
    assert "timing:" in result.output
    assert "busybox-1.36.1-r0:do_compile" in result.output
    assert "40.0s" in result.output

    # pressure: CPU dominates (avg 17.5%) and the verdict names it.
    assert "pressure:" in result.output
    assert "CPU pressure dominated" in result.output

    # disk: growth figure and the DiskFull event both surface.
    assert "disk:" in result.output
    assert "growth: 500000 bytes" in result.output
    assert "disk full:" in result.output


@pytest.mark.unit
def test_insights_single_flag_renders_only_that_section(
    runner: _CliRunner, nxp_workspace: Path, insights_run_dir: Path
) -> None:
    """``--sstate`` alone renders only the sstate section, not the other three."""
    result = runner.invoke(app, ["insights", "--sstate", "--workspace", str(nxp_workspace)])
    assert result.exit_code == 0, result.output
    assert "sstate:" in result.output
    assert "timing:" not in result.output
    assert "pressure:" not in result.output
    assert "disk:" not in result.output


@pytest.mark.unit
def test_insights_names_the_run(runner: _CliRunner, nxp_workspace: Path, insights_run_dir: Path) -> None:
    """The command always names the run it reported on."""
    result = runner.invoke(app, ["insights", "--workspace", str(nxp_workspace)])
    assert result.exit_code == 0, result.output
    assert RUN_ID in result.output


@pytest.mark.unit
def test_insights_renders_the_buildstats_derived_sections(
    runner: _CliRunner, nxp_workspace: Path, insights_run_dir: Path
) -> None:
    """The join, CPU-floor and concurrency-floor sections reach the page.

    The fixture workspace has no buildstats tree and this command supplies no
    dependency source, so all three degrade - which is the state under test.
    A section wired into the report but never rendered is indistinguishable
    from one that was never wired at all, and the degraded note is the only
    thing that tells a reader why no floor appeared.
    """
    result = runner.invoke(app, ["insights", "--timing", "--workspace", str(nxp_workspace)])
    assert result.exit_code == 0, result.output

    assert "buildstats join:" in result.output
    assert "cpu floor:" in result.output
    assert "concurrency floor:" in result.output

    # Rich hard-wraps to the terminal width, so match against a whitespace-
    # normalized copy rather than the raw output - otherwise this assertion
    # passes or fails on the console width the suite happens to run at.
    flat = " ".join(result.output.split())
    assert "concurrency floor unavailable: the CPU floor is unavailable" in flat


@pytest.mark.unit
def test_insights_renders_the_churn_section_when_it_degrades(
    runner: _CliRunner, nxp_workspace: Path, insights_run_dir: Path
) -> None:
    """The churn section reaches the page even with no buildstats tree present.

    A section wired into the report but never rendered is indistinguishable from
    one that was never wired at all.
    """
    result = runner.invoke(app, ["insights", "--timing", "--workspace", str(nxp_workspace)])
    assert result.exit_code == 0, result.output

    assert "task churn:" in result.output
    flat = " ".join(result.output.split())
    assert "task churn unavailable: tree absent" in flat


#: The fixture build runs from epoch 1000.0 to 1040.0 (``_build_eventlog``). A
#: capture directory is named in LOCAL time, so derive the name from the window
#: rather than hardcoding one - a literal would correlate only in the timezone
#: it was written in.
BUILD_STARTED = 1000.0
BUILD_COMPLETED = 1040.0


def _capture_name(epoch: float) -> str:
    """Name a capture directory the way the build container writes it - in UTC.

    Local time here would put the fixture 6 h from its own window on any host
    that is not UTC, which is the exact defect that made correlation fail on
    every real run: bitbake writes the name from the container's clock (UTC
    under kas-container) while the analysing host runs UTC-6.
    """
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y%m%d%H%M%S")


#: Every ``(recipe, task)`` the fixture event log executes with a usable
#: duration. Writing a record for all of them is what lets the 95% join gate
#: pass, which is the precondition for any CPU-derived figure rendering at all.
EXECUTED = (
    ("busybox-1.36.1-r0", "do_compile"),
    ("coreutils-9.4-r0", "do_compile"),
    ("busybox-1.36.1-r0", "do_populate_sysroot_setscene"),
)


def _write_capture(root: Path, entries: tuple[tuple[str, str], ...]) -> None:
    for recipe, task in entries:
        task_dir = root / recipe
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / task).write_text(
            "Elapsed time: 40.00 seconds\n"
            "rusage ru_utime: 1.5\n"
            "rusage ru_stime: 0.5\n"
            "rusage ru_minflt: 7023117\n"
            "rusage ru_majflt: 40\n"
            "Child rusage ru_utime: 9.0\n"
            "Child rusage ru_stime: 1.0\n"
            "Child rusage ru_minflt: 214243919\n"
            "Child rusage ru_majflt: 60\n"
            "IO syscr: 1000\n"
            "IO syscw: 500\n"
            "IO write_bytes: 2000000000\n",
            encoding="utf-8",
        )


@pytest.mark.unit
def test_insights_renders_populated_churn_columns(
    runner: _CliRunner, nxp_workspace: Path, insights_run_dir: Path
) -> None:
    """A real capture under the resolved TMPDIR produces non-zero columns.

    The shared eventlog fixture executes one ``do_compile`` (busybox); the zlib
    record written here is never executed by that fixture, so it also pins the
    rule that an unexecuted record contributes nothing to the columns.
    """
    buildstats = nxp_workspace / "nxp" / "build" / "tmp" / "buildstats"
    _write_capture(buildstats / _capture_name(BUILD_STARTED), (EXECUTED[0], ("zlib-1.3-r0", "do_compile")))

    result = runner.invoke(app, ["insights", "--timing", "--workspace", str(nxp_workspace)])
    assert result.exit_code == 0, result.output

    flat = " ".join(result.output.split())
    assert "task churn over 1 of" in flat
    # Self+child summed, per the section's stated basis: a self-only sum would
    # render 7,023,117 here and still look like a populated column.
    assert f"{7_023_117 + 214_243_919:,}" in flat
    assert "7,023,117" not in flat
    # The capture the columns came from is named on the PUBLISHED path, not only
    # on the refusing ones - the figure a reader might act on is the one whose
    # provenance they need. Rich hard-wraps a long path mid-token, so squash all
    # whitespace rather than only collapsing runs of it.
    squashed = "".join(result.output.split())
    assert f"fromcapture{buildstats / _capture_name(BUILD_STARTED)}" in squashed


@pytest.mark.unit
def test_insights_refuses_a_capture_from_a_different_build(
    runner: _CliRunner, nxp_workspace: Path, insights_run_dir: Path
) -> None:
    """A capture outside this run's build window is refused, not silently joined.

    ``insights`` takes an explicit run id, so taking the NEWEST capture joins an
    older run against a later build's records. Consecutive builds of one target
    execute a near-identical ``(PN, task)`` set, so that join passes the 95% gate
    at close to 100% and publishes a floor over the wrong build - the exact case
    the gate exists to catch, defeated by capture selection.
    """
    buildstats = nxp_workspace / "nxp" / "build" / "tmp" / "buildstats"
    _write_capture(buildstats / _capture_name(BUILD_COMPLETED + 86_400), EXECUTED)

    result = runner.invoke(app, ["insights", "--timing", "--workspace", str(nxp_workspace)])
    assert result.exit_code == 0, result.output

    squashed = "".join(result.output.split())
    assert "nocapturebelongstothisrun" in squashed
    # No CPU-derived figure may reach the page over a capture that could not be
    # shown to belong here.
    assert f"{7_023_117 + 214_243_919:,}" not in squashed


@pytest.mark.unit
def test_insights_reads_the_host_block_the_build_host_recorded(
    runner: _CliRunner, nxp_workspace: Path, insights_run_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CPU floor's divisor comes from the persisted artifact, not this machine.

    ``bitbake-events.json`` is normalized on the BUILD host, so its ``host``
    block records the machine that ran the build. Re-normalizing the raw
    ``bitbake_eventlog.json`` here instead re-runs ``_host_block`` on the
    ANALYSING host, and the floor then divides by this machine's core count
    while printing "recorded at capture" beside it - a false provenance claim.
    Patching ``os.cpu_count`` proves which side the number came from.
    """
    buildstats = nxp_workspace / "nxp" / "build" / "tmp" / "buildstats"
    _write_capture(buildstats / _capture_name(BUILD_STARTED), EXECUTED)
    artifact = eventlog.normalize(insights_run_dir / "bitbake_eventlog.json")
    artifact["host"] = {"cpu_count": 128, "bb_number_threads": 128, "parallel_make": 128}
    (insights_run_dir / "bitbake-events.json").write_text(json.dumps(artifact), encoding="utf-8")
    monkeypatch.setattr("os.cpu_count", lambda: 3)

    result = runner.invoke(app, ["insights", "--timing", "--workspace", str(nxp_workspace)])
    assert result.exit_code == 0, result.output

    squashed = "".join(result.output.split())
    assert "/128cores" in squashed
    assert "/3cores" not in squashed


@pytest.mark.unit
def test_insights_withholds_the_floor_when_only_the_raw_log_survives(
    runner: _CliRunner, nxp_workspace: Path, insights_run_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fallback path must not synthesize a host block either.

    Preferring the persisted ``bitbake-events.json`` removed the false
    provenance from the PRIMARY path only. With no persisted artifact,
    ``eventlog.normalize`` runs here and builds ``host.cpu_count`` from
    ``os.cpu_count()`` on the ANALYSING machine, which the floor then labels
    "recorded at capture" - the same defect, one branch over. Patching
    ``os.cpu_count`` to a value no machine has is what tells the two apart: a
    floor divided by it could only have come from this host.
    """
    buildstats = nxp_workspace / "nxp" / "build" / "tmp" / "buildstats"
    _write_capture(buildstats / _capture_name(BUILD_STARTED), EXECUTED)
    assert not (insights_run_dir / "bitbake-events.json").exists()
    monkeypatch.setattr("os.cpu_count", lambda: 4242)

    result = runner.invoke(app, ["insights", "--timing", "--workspace", str(nxp_workspace)])
    assert result.exit_code == 0, result.output

    squashed = "".join(result.output.split())
    assert "recordsnobuild-hostcorecount" in squashed
    assert "4242" not in squashed
    assert "cores(buildhostcpu_count" not in squashed
    # The join itself still runs - only the divisor is withheld, so a reader can
    # still see the capture correlated and the churn columns are still real.
    assert "buildstatsjoin100.0%" in squashed


@pytest.mark.unit
def test_insights_withholds_the_floor_for_a_pre_schema_5_artifact(
    runner: _CliRunner, nxp_workspace: Path, insights_run_dir: Path
) -> None:
    """A persisted artifact with no host block degrades; it is not back-filled.

    A run predating schema 5 records ``host: None``. Synthesizing a block on the
    read path would trade a withheld floor for a falsely-attributed one, which
    is the trade this whole section refuses.
    """
    buildstats = nxp_workspace / "nxp" / "build" / "tmp" / "buildstats"
    _write_capture(buildstats / _capture_name(BUILD_STARTED), EXECUTED)
    artifact = eventlog.normalize(insights_run_dir / "bitbake_eventlog.json")
    artifact["host"] = None
    (insights_run_dir / "bitbake-events.json").write_text(json.dumps(artifact), encoding="utf-8")

    result = runner.invoke(app, ["insights", "--timing", "--workspace", str(nxp_workspace)])
    assert result.exit_code == 0, result.output

    squashed = "".join(result.output.split())
    assert "recordsnobuild-hostcorecount" in squashed
    assert "cores(buildhostcpu_count" not in squashed
