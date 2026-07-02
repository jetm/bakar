"""Tests for the ``bakar cluster-info`` command.

Drives the command through the Typer ``CliRunner``. The scheduler probe
(``probe_cluster``) is monkeypatched on the command module so no real
``sccache --dist-status`` subprocess runs; ``probe_cluster`` itself is exercised
against a fake subprocess in ``test_sccache_dist.py``.

Importing ``bakar.commands.cluster_info`` registers the command on the shared
``app``; this module imports it to stay self-contained.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

import bakar.commands._app as _state
import bakar.commands.cluster_info as cluster_info_module
from bakar.cli import app
from bakar.diagnostics import ClusterCapacity, ClusterReport

if TYPE_CHECKING:
    from typer.testing import CliRunner as _CliRunner

pytestmark = pytest.mark.unit


@pytest.fixture
def runner() -> _CliRunner:
    from typer.testing import CliRunner

    return CliRunner()


@pytest.fixture(autouse=True)
def _stub_build_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep cluster-info tests hermetic: default the daemon probe to not-running.

    probe_build_daemon now falls back to the host UDS daemon, so an unmocked
    call would depend on whether a real sccache daemon is running on the test
    host. Stub it to not-running by default; the per-language tests override it.
    """
    from bakar.diagnostics import BuildDaemonReport

    monkeypatch.setattr(cluster_info_module, "probe_build_daemon", lambda: BuildDaemonReport(running=False))


def _reachable(servers: object = None) -> ClusterReport:
    return ClusterReport(
        reachable=True,
        capacity=ClusterCapacity(num_servers=2, num_cpus=64, in_progress=7, servers=servers),
    )


def test_cluster_info_is_registered(runner: _CliRunner) -> None:
    result = runner.invoke(app, ["cluster-info", "--help"])

    assert result.exit_code == 0
    assert "cluster-info" in result.output


def test_cluster_info_reports_aggregate(runner: _CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cluster_info_module, "probe_cluster", lambda _url: _reachable())

    result = runner.invoke(app, ["cluster-info"])

    assert result.exit_code == 0
    # Bind each value to its label so a field-swap regression is caught.
    assert "build servers: 2" in result.output
    assert "cpus: 64" in result.output
    assert "jobs in progress: 7" in result.output


def test_cluster_info_json_shape(runner: _CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cluster_info_module, "probe_cluster", lambda _url: _reachable())

    result = runner.invoke(app, ["cluster-info", "--json"])

    assert result.exit_code == 0
    doc = json.loads(result.stdout)
    assert doc["reachable"] is True
    assert doc["capacity"] == {
        "num_servers": 2,
        "num_cpus": 64,
        "in_progress": 7,
        "servers": None,
    }


def test_cluster_info_exits_1_when_unreachable(runner: _CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cluster_info_module,
        "probe_cluster",
        lambda _url: ClusterReport(reachable=False, error="scheduler unreachable"),
    )

    result = runner.invoke(app, ["cluster-info"])

    assert result.exit_code == 1
    assert "unreachable" in result.output


def test_cluster_info_json_exits_1_when_sccache_absent(runner: _CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cluster_info_module,
        "probe_cluster",
        lambda _url: ClusterReport(reachable=False, error="sccache binary not found on PATH"),
    )

    result = runner.invoke(app, ["cluster-info", "--json"])

    assert result.exit_code == 1
    doc = json.loads(result.stdout)
    assert doc["reachable"] is False
    assert doc["capacity"] is None
    assert "not found" in doc["error"]


def test_scheduler_flag_overrides_config(runner: _CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def _capture(url: str | None) -> ClusterReport:
        captured["url"] = url
        return _reachable()

    monkeypatch.setattr(cluster_info_module, "probe_cluster", _capture)

    result = runner.invoke(app, ["cluster-info", "--scheduler", "http://flag:10600"])

    assert result.exit_code == 0
    assert captured["url"] == "http://flag:10600"


def test_global_sccache_scheduler_used_when_no_command_flag(
    runner: _CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no --scheduler, the global --sccache-scheduler wins over config."""
    from bakar.user_config import UserConfig

    captured: dict[str, object] = {}

    def _capture(url: str | None) -> ClusterReport:
        captured["url"] = url
        return _reachable()

    monkeypatch.setattr(cluster_info_module, "probe_cluster", _capture)
    monkeypatch.setattr(
        _state,
        "_load_user_config_safe",
        lambda: UserConfig(sccache_scheduler_url="http://config:10600"),
    )

    result = runner.invoke(app, ["--sccache-scheduler", "http://global:10600", "cluster-info"])

    assert result.exit_code == 0
    assert captured["url"] == "http://global:10600"


def test_scheduler_falls_back_to_user_config(runner: _CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    from bakar.user_config import UserConfig

    captured: dict[str, object] = {}

    def _capture(url: str | None) -> ClusterReport:
        captured["url"] = url
        return _reachable()

    monkeypatch.setattr(cluster_info_module, "probe_cluster", _capture)
    monkeypatch.setattr(
        _state,
        "_load_user_config_safe",
        lambda: UserConfig(sccache_scheduler_url="http://config:10600"),
    )

    result = runner.invoke(app, ["cluster-info"])

    assert result.exit_code == 0
    assert captured["url"] == "http://config:10600"


def test_cluster_info_prints_node_table_when_servers_present(
    runner: _CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cluster_info_module,
        "probe_cluster",
        lambda _url: _reachable(
            servers=[
                {"id": "10.42.0.1:10501", "num_cpus": 32, "in_progress": 2},
                {"id": "10.42.0.2:10501", "num_cpus": 32, "in_progress": 0},
            ]
        ),
    )

    result = runner.invoke(app, ["cluster-info"])

    assert result.exit_code == 0
    assert "10.42.0.1:10501 - 32 cpus, 2 job(s)" in result.output
    assert "10.42.0.2:10501 - 32 cpus, 0 job(s)" in result.output


def _daemon_with_langs():  # type: ignore[no-untyped-def]
    from bakar.diagnostics import BuildDaemonReport

    return BuildDaemonReport(
        running=True,
        container=None,
        cache_hits=3104,
        cache_misses=34007,
        cache_hits_by_lang={"C/C++": 3104},
        cache_misses_by_lang={"Rust": 400, "C/C++": 33607},
        distributed=28909,
        dist_errors=739,
        per_node=(("10.42.0.2:10501", 14505), ("192.168.8.172:10501", 14404)),
    )


def test_cluster_info_json_carries_per_language(runner: _CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    """The --json build_daemon block exposes the per-language + per-node breakdown."""
    monkeypatch.setattr(cluster_info_module, "probe_cluster", lambda _url: _reachable())
    monkeypatch.setattr(cluster_info_module, "probe_build_daemon", _daemon_with_langs)

    result = runner.invoke(app, ["cluster-info", "--json"])

    assert result.exit_code == 0
    bd = json.loads(result.stdout)["build_daemon"]
    assert bd["misses_by_lang"] == {"Rust": 400, "C/C++": 33607}
    assert bd["hits_by_lang"] == {"C/C++": 3104}
    assert bd["per_node"] == {"10.42.0.2:10501": 14505, "192.168.8.172:10501": 14404}


def test_cluster_info_human_shows_per_language(runner: _CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    """The human output prints a per-language hit/miss line for each language present."""
    monkeypatch.setattr(cluster_info_module, "probe_cluster", lambda _url: _reachable())
    monkeypatch.setattr(cluster_info_module, "probe_build_daemon", _daemon_with_langs)

    result = runner.invoke(app, ["cluster-info"])

    assert result.exit_code == 0
    assert "cache[Rust]:" in result.output
    assert "0/400 hit/miss" in result.output
