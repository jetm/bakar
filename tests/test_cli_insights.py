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
from typing import TYPE_CHECKING

import pytest

import bakar.commands.insights as insights_module  # noqa: F401  (registers the command on import)
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


@pytest.mark.unit
def test_insights_renders_populated_churn_columns(
    runner: _CliRunner, nxp_workspace: Path, insights_run_dir: Path
) -> None:
    """A real capture under the resolved TMPDIR produces non-zero columns.

    The shared eventlog fixture executes one ``do_compile`` (busybox); the zlib
    record written here is never executed by that fixture, so it also pins the
    rule that an unexecuted record contributes nothing to the columns.
    """
    capture = nxp_workspace / "nxp" / "build" / "tmp" / "buildstats" / "20260101000000"
    for recipe in ("busybox-1.36.1-r0", "zlib-1.3-r0"):
        task_dir = capture / recipe
        task_dir.mkdir(parents=True)
        (task_dir / "do_compile").write_text(
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

    result = runner.invoke(app, ["insights", "--timing", "--workspace", str(nxp_workspace)])
    assert result.exit_code == 0, result.output

    flat = " ".join(result.output.split())
    assert "task churn over 1 of" in flat
    # Self+child summed, per the section's stated basis: a self-only sum would
    # render 7,023,117 here and still look like a populated column.
    assert f"{7_023_117 + 214_243_919:,}" in flat
    assert "7,023,117" not in flat
