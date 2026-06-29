"""Tests for the ``bakar monitor`` command.

Drives the command through the Typer ``CliRunner``. Both heavy probes
(``probe_cluster`` and ``probe_build_daemon``) are monkeypatched on the
command module so no real ``sccache``/``docker`` subprocess runs, and the
bitbake event-log reader (``normalize``) is patched to return synthetic task
data so a base64-pickled raw log is not needed. The throttle test exercises the
``_DaemonProbe`` window in isolation.

Workspace shape mirrors ``test_cli_log.py``: tests build
``<tmp_path>/nxp/build/runs/<run-id>/`` so workspace detection picks NXP and
``cfg.runs_dir`` (= ``workspace/nxp/build/runs``) finds the run.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

import bakar.commands.monitor as monitor_module
from bakar.cli import app
from bakar.diagnostics import BuildDaemonReport, ClusterCapacity, ClusterReport

if TYPE_CHECKING:
    from pathlib import Path

    from typer.testing import CliRunner as _CliRunner

pytestmark = pytest.mark.unit

_RUN_ID = "20260601-120000"


@pytest.fixture
def runner() -> _CliRunner:
    from typer.testing import CliRunner

    return CliRunner()


@pytest.fixture
def nxp_workspace_with_run(tmp_path: Path) -> Path:
    """NXP workspace with one run dir at the NXP layout (no raw event log)."""
    run = tmp_path / "nxp" / "build" / "runs" / _RUN_ID
    run.mkdir(parents=True)
    return tmp_path


def _reachable_cluster() -> ClusterReport:
    return ClusterReport(
        reachable=True,
        capacity=ClusterCapacity(num_servers=2, num_cpus=64, in_progress=7, servers=None),
    )


def _running_daemon() -> BuildDaemonReport:
    return BuildDaemonReport(
        running=True,
        container="abc123",
        cache_hits=10,
        cache_misses=4,
        distributed=4,
        dist_errors=0,
        per_node=(("10.42.0.2:10501", 4),),
    )


def _synthetic_artifact() -> dict[str, Any]:
    """A normalized event-log artifact with one running, one done, one failed task."""
    return {
        "schema_version": 1,
        "build": {
            "started": None,
            "completed": None,
            "outcome": "unknown",
            "tasks_total": 100,
            "tasks_completed": 60,
            "tasks_active": 1,
        },
        "tasks": [
            {"recipe": "busybox-1.36.1-r0", "task": "do_compile", "outcome": "succeeded", "started": 1.0},
            {"recipe": "zlib-1.3-r0", "task": "do_configure", "outcome": None, "started": 2.0},
            {"recipe": "linux-imx-6.12-r0", "task": "do_compile", "outcome": "failed", "started": 3.0},
            {
                "recipe": "glibc-locale-2.39-r0",
                "task": "do_packagedata_setscene",
                "outcome": "failed_silent",
                "started": 4.0,
            },
        ],
        "setscene": {"covered": 0, "notcovered": 0, "total": 0, "per_recipe": []},
        "failures": [{"recipe": "linux-imx-6.12-r0", "task": "do_compile", "logfile": "/x/log", "errprinted": True}],
    }


@pytest.fixture
def patched_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch both heavy probes and the event-log reader on the monitor module."""
    monkeypatch.setattr(monitor_module, "probe_cluster", lambda _url: _reachable_cluster())
    monkeypatch.setattr(monitor_module, "probe_build_daemon", _running_daemon)
    monkeypatch.setattr(monitor_module, "normalize", lambda _path: _synthetic_artifact())
    # is_build_running shells out to /proc; force a deterministic "finished".
    monkeypatch.setattr(monitor_module, "is_build_running", lambda _run_dir: (False, None, False))


def test_monitor_is_registered(runner: _CliRunner) -> None:
    result = runner.invoke(app, ["monitor", "--help"])

    assert result.exit_code == 0
    assert "monitor" in result.output


def test_json_once_emits_cluster_and_build(
    runner: _CliRunner,
    nxp_workspace_with_run: Path,
    patched_probes: None,
) -> None:
    """``--json --once`` writes one valid JSON doc carrying cluster + build keys."""
    result = runner.invoke(
        app,
        ["monitor", "--json", "--once", "--workspace", str(nxp_workspace_with_run)],
    )

    assert result.exit_code == 0, result.stderr
    doc = json.loads(result.stdout)
    assert doc["run"] == _RUN_ID
    assert doc["cluster"]["capacity"] == {
        "num_servers": 2,
        "num_cpus": 64,
        "in_progress": 7,
        "servers": None,
    }
    assert doc["build_daemon"]["verdict"] == "DISTRIBUTING"
    build = doc["build"]
    # Runqueue progress comes from the synthetic stats: 60 of 100, 40 left.
    assert build["tasks_total"] == 100
    assert build["tasks_done"] == 60
    assert build["tasks_remaining"] == 40
    # Only the real do_compile failure counts as failed; the failed_silent
    # setscene rejection is a recovered cache miss, reported separately.
    assert build["tasks_failed"] == 1
    assert build["tasks_setscene_rerun"] == 1
    assert build["tasks_running"] == 1
    assert build["running"] == [{"recipe": "zlib-1.3-r0", "task": "do_configure"}]
    assert build["live"] is False
    # Elapsed is derived from the run-dir name (BuildStarted carries no time),
    # so it is populated even though build.started is None.
    assert build["elapsed_seconds"] is not None and build["elapsed_seconds"] > 0


def test_json_once_omits_decoration_on_stdout(
    runner: _CliRunner,
    nxp_workspace_with_run: Path,
    patched_probes: None,
) -> None:
    """stdout is pure JSON; nothing else is written there in --json mode."""
    result = runner.invoke(
        app,
        ["monitor", "--json", "--once", "--workspace", str(nxp_workspace_with_run)],
    )

    assert result.exit_code == 0
    # The whole stdout must parse as a single JSON document.
    json.loads(result.stdout)


def test_progress_falls_back_before_runqueue_total_known(
    runner: _CliRunner,
    nxp_workspace_with_run: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Before the runqueue total is known (no runQueueTaskStarted yet), done
    falls back to the succeeded-task count and remaining is null."""
    artifact = _synthetic_artifact()
    artifact["build"]["tasks_total"] = None
    artifact["build"]["tasks_completed"] = None
    artifact["build"]["tasks_active"] = None
    monkeypatch.setattr(monitor_module, "probe_cluster", lambda _url: _reachable_cluster())
    monkeypatch.setattr(monitor_module, "probe_build_daemon", _running_daemon)
    monkeypatch.setattr(monitor_module, "normalize", lambda _path: artifact)
    monkeypatch.setattr(monitor_module, "is_build_running", lambda _run_dir: (False, None, False))

    result = runner.invoke(
        app,
        ["monitor", "--json", "--once", "--workspace", str(nxp_workspace_with_run)],
    )

    assert result.exit_code == 0, result.stderr
    build = json.loads(result.stdout)["build"]
    assert build["tasks_total"] is None
    assert build["tasks_done"] == 1  # fallback: one succeeded task seen
    assert build["tasks_remaining"] is None


def test_no_active_run_json_exits_nonzero(
    runner: _CliRunner,
    tmp_path: Path,
) -> None:
    """No build/runs dir: --json exits 1 with a machine-readable error doc."""
    (tmp_path / "nxp").mkdir()

    result = runner.invoke(app, ["monitor", "--json", "--workspace", str(tmp_path)])

    assert result.exit_code == 1
    doc = json.loads(result.stdout)
    assert "no runs" in doc["error"]


def test_no_active_run_human_exits_nonzero(
    runner: _CliRunner,
    tmp_path: Path,
) -> None:
    """No build/runs dir: the human path exits 1 with a clear stderr message."""
    (tmp_path / "nxp").mkdir()

    result = runner.invoke(app, ["monitor", "--workspace", str(tmp_path)])

    assert result.exit_code == 1
    assert "no runs yet" in result.stderr


def test_watch_requires_json(runner: _CliRunner, nxp_workspace_with_run: Path) -> None:
    """``--watch`` without ``--json`` exits 2 (it is NDJSON-only)."""
    result = runner.invoke(
        app,
        ["monitor", "--watch", "--workspace", str(nxp_workspace_with_run)],
    )

    assert result.exit_code == 2
    assert "--watch is only meaningful with --json" in result.stderr


def test_watch_emits_ndjson_and_stops_when_finished(
    runner: _CliRunner,
    nxp_workspace_with_run: Path,
    patched_probes: None,
) -> None:
    """``--json --watch`` emits one compact NDJSON line then stops (build finished)."""
    result = runner.invoke(
        app,
        ["monitor", "--json", "--watch", "--workspace", str(nxp_workspace_with_run)],
    )

    assert result.exit_code == 0, result.stderr
    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    # The synthetic build reports live=False on the first probe, so the watch
    # loop emits exactly one snapshot and returns.
    assert len(lines) == 1
    doc = json.loads(lines[0])
    # Compact: json.dumps(obj) has no indentation.
    assert lines[0] == json.dumps(doc)


def test_daemon_probe_throttle_caches_within_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_DaemonProbe`` calls the heavy probe at most once inside its window."""
    calls = {"n": 0}

    def _count() -> BuildDaemonReport:
        calls["n"] += 1
        return BuildDaemonReport(running=False)

    monkeypatch.setattr(monitor_module, "probe_build_daemon", _count)

    fake_now = {"t": 100.0}
    monkeypatch.setattr(monitor_module.time, "monotonic", lambda: fake_now["t"])

    probe = monitor_module._DaemonProbe(throttle=3.0)
    probe.get()
    probe.get()
    fake_now["t"] = 102.0  # still inside the 3s window
    probe.get()

    assert calls["n"] == 1, "probe_build_daemon called more than once within the throttle window"

    fake_now["t"] = 104.0  # past the window
    probe.get()
    assert calls["n"] == 2, "probe_build_daemon should re-probe once the window elapses"


def test_unreachable_cluster_does_not_crash_json(
    runner: _CliRunner,
    nxp_workspace_with_run: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreachable scheduler is reported in the doc, not raised."""
    monkeypatch.setattr(
        monitor_module,
        "probe_cluster",
        lambda _url: ClusterReport(reachable=False, error="scheduler unreachable"),
    )
    monkeypatch.setattr(monitor_module, "probe_build_daemon", lambda: BuildDaemonReport(running=False))
    monkeypatch.setattr(monitor_module, "normalize", lambda _path: _synthetic_artifact())
    monkeypatch.setattr(monitor_module, "is_build_running", lambda _run_dir: (False, None, False))

    result = runner.invoke(
        app,
        ["monitor", "--json", "--once", "--workspace", str(nxp_workspace_with_run)],
    )

    assert result.exit_code == 0, result.stderr
    doc = json.loads(result.stdout)
    assert doc["cluster"]["reachable"] is False
    assert doc["cluster"]["capacity"] is None
    assert "unreachable" in doc["cluster"]["error"]
    assert doc["build_daemon"] is None
