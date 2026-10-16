"""Tests for the ``bakar sched-triage`` command.

The scheduler journal is replaced by a fixed list of lines (``_journal_lines``
is patched, so no ``journalctl`` runs) and the client log is a tmp file, so the
command reads neither the real journal nor the real environment. Assertions
are on the exact JSON the command emits.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

import bakar.commands.sched_triage as triage_cmd
from bakar.cli import app
from bakar.user_config import UserConfig

if TYPE_CHECKING:
    from pathlib import Path

    from typer.testing import CliRunner as _CliRunner

pytestmark = pytest.mark.unit

_JOURNAL = [
    "dist-alloc: job 1 -> ServerId(192.168.8.172:10501) (now 6/32 jobs, load 0.6); "
    "candidates [(ServerId(192.168.8.172:10501), 0.5, 5, 32), (ServerId(10.42.0.2:10501), 0.0, 0, 32)]",
    "dist-alloc: job 2 -> ServerId(10.42.0.2:10501) (now 1/32 jobs, load 0.03); "
    "candidates [(ServerId(10.42.0.2:10501), 0.0, 0, 32), (ServerId(192.168.8.172:10501), 0.5, 5, 32)]",
    'dist-status poll: 0 in-progress jobs; servers [("192.168.8.172:10501", 0, 32), ("10.42.0.2:10501", 0, 32)]',
    'dist-status poll: 32 in-progress jobs; servers [("192.168.8.172:10501", 16, 32), ("10.42.0.2:10501", 16, 32)]',
]

_CLIENT_LOG = "\n".join(
    [
        "[a.o]: dist-job done on 192.168.8.172:10501 in 5841ms "
        "(put_tc 1335ms, alloc 200ms, submit 0ms, run+fetch 4305ms, in_flight 85)",
        "[b.o]: dist-job done on 10.42.0.2:10501 in 4000ms "
        "(put_tc 1000ms, alloc 100ms, submit 0ms, run+fetch 2900ms, in_flight 40)",
        "[conftest.o]: Compiling locally (not eligible for distributed compilation)",
        "[c.o]: Could not perform distributed compile, falling back to local: "
        "Insufficient capacity across 2 available servers: Failed to allocate job",
    ]
)


_STATUS_ONLY = [line for line in _JOURNAL if line.startswith("dist-status")]
_LOCAL_ONLY_LOG = "[conftest.o]: Compiling locally (not eligible for distributed compilation)\n"


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch: pytest.MonkeyPatch) -> None:
    """Load a default user config (not ~/.config/bakar) and clear the env fallback."""
    monkeypatch.setattr("bakar.commands._app._load_user_config_safe", UserConfig)
    monkeypatch.delenv("SCCACHE_ERROR_LOG", raising=False)


def _serve(monkeypatch: pytest.MonkeyPatch, lines: list[str]) -> list[tuple[str, str]]:
    """Serve ``lines`` as the journal and record the (unit, since) it was asked for."""
    calls: list[tuple[str, str]] = []

    def fake(unit: str, since: str) -> list[str]:
        calls.append((unit, since))
        return list(lines)

    monkeypatch.setattr(triage_cmd, "_journal_lines", fake)
    return calls


def _doc(runner: _CliRunner, *args: str) -> dict:
    result = runner.invoke(app, ["sched-triage", "--json", *args])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def test_json_report_from_journal_and_client_log(
    runner: _CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Journal status polls and a client log are folded into the JSON report."""
    calls = _serve(monkeypatch, _STATUS_ONLY)
    log = tmp_path / "client.log"
    log.write_text(_LOCAL_ONLY_LOG)

    doc = _doc(runner, "--client-log", str(log), "--since", "2 hours ago", "--unit", "my.service")

    assert calls == [("my.service", "2 hours ago")]
    assert doc["since"] == "2 hours ago"
    assert doc["client_log"] == str(log)
    assert doc["routing"]["total"] == 0
    assert doc["saturation"]["samples"] == 2
    assert doc["saturation"]["ceiling"] == 64
    assert doc["saturation"]["mean_util_pct"] == 25.0
    assert doc["saturation"]["idle_pct"] == 50.0
    assert doc["client"]["jobs"] == 0
    assert doc["client"]["not_eligible"] == 1
    assert "conditioned" not in doc


def test_env_fallback_for_client_log(runner: _CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without --client-log the path comes from $SCCACHE_ERROR_LOG."""
    _serve(monkeypatch, _STATUS_ONLY)
    log = tmp_path / "env-client.log"
    log.write_text(_LOCAL_ONLY_LOG)
    monkeypatch.setenv("SCCACHE_ERROR_LOG", str(log))

    doc = _doc(runner)

    assert doc["client_log"] == str(log)
    assert doc["client"]["not_eligible"] == 1


def test_client_log_option_beats_env(runner: _CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--client-log wins over $SCCACHE_ERROR_LOG."""
    _serve(monkeypatch, _STATUS_ONLY)
    env_log = tmp_path / "env.log"
    env_log.write_text(_LOCAL_ONLY_LOG * 3)
    opt_log = tmp_path / "opt.log"
    opt_log.write_text(_LOCAL_ONLY_LOG)
    monkeypatch.setenv("SCCACHE_ERROR_LOG", str(env_log))

    doc = _doc(runner, "--client-log", str(opt_log))

    assert doc["client_log"] == str(opt_log)
    assert doc["client"]["not_eligible"] == 1


def test_no_client_log_reports_zero_client_section(runner: _CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    """No option and no env var: client_log is null and the client section is empty."""
    _serve(monkeypatch, _STATUS_ONLY)

    doc = _doc(runner)

    assert doc["client_log"] is None
    assert doc["client"]["jobs"] == 0
    assert doc["client"]["not_eligible"] == 0
    assert doc["saturation"]["samples"] == 2


def test_unreadable_client_log_reports_zero_client_section(
    runner: _CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A client log path that does not exist yields an empty client section, exit 0."""
    _serve(monkeypatch, _STATUS_ONLY)
    missing = tmp_path / "missing.log"

    doc = _doc(runner, "--client-log", str(missing))

    assert doc["client_log"] == str(missing)
    assert doc["client"]["not_eligible"] == 0


def test_json_report_with_populated_counters(
    runner: _CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Populated Counter fields serialise as str-keyed dicts, ordered by count then key."""
    _serve(monkeypatch, _JOURNAL)
    log = tmp_path / "client.log"
    log.write_text(_CLIENT_LOG + "\n")

    doc = _doc(runner, "--client-log", str(log))

    routing = doc["routing"]
    assert routing["per_node_chosen"] == {"10.42.0.2:10501": 1, "192.168.8.172:10501": 1}
    assert list(routing["per_node_chosen"]) == ["10.42.0.2:10501", "192.168.8.172:10501"]
    assert routing["total_by_bucket"] == {"low": 2}
    assert routing["misroutes_by_bucket"] == {"low": 1}
    assert routing["idle_skips_by_bucket"] == {"low": 1}
    assert routing["misroutes"] == 1
    client = doc["client"]
    assert client["per_node_jobs"] == {"10.42.0.2:10501": 1, "192.168.8.172:10501": 1}
    assert client["fallback_reasons"] == {"gate-full (insufficient cluster capacity)": 1}
    assert client["rust_error_codes"] == {}


def test_text_report_sections(runner: _CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The non-JSON report prints the routing, saturation and client headline lines."""
    _serve(monkeypatch, _JOURNAL)
    log = tmp_path / "client.log"
    log.write_text(_CLIENT_LOG + "\n")

    result = runner.invoke(app, ["sched-triage", "--client-log", str(log)])

    assert result.exit_code == 0, result.output
    assert "allocations: 2" in result.output
    assert "misroutes: 1 (50.0%)" in result.output
    assert "polls: 2, ceiling: 64 cores" in result.output
    assert "distributed jobs: 2" in result.output
    assert "1x gate-full (insufficient cluster capacity)" in result.output
    assert "no remote rustc errors seen in the client log" in result.output
