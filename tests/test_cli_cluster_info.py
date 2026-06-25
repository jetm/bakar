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
        lambda _url: _reachable(servers=["server-a", "server-b"]),
    )

    result = runner.invoke(app, ["cluster-info"])

    assert result.exit_code == 0
    assert "server-a" in result.output
    assert "server-b" in result.output
