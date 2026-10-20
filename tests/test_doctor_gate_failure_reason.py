"""The doctor gate records the blocking checks' text on its failing event."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest
import typer

import bakar.commands._app as app_state
import bakar.diagnostics as diagnostics
from bakar.commands._helpers import _run_doctor_gate
from bakar.commands.status import _failure
from bakar.config import BuildConfig
from bakar.diagnostics import CheckResult, Severity, Status
from bakar.observability import RunLogger
from bakar.triage import analyse
from tests.test_run_status import RUN_ID, _run_dir, env  # noqa: F401

pytestmark = pytest.mark.unit


def _cfg() -> BuildConfig:
    return BuildConfig(
        workspace=Path("/tmp"),
        bsp_family="nxp",
        machine="m",
        distro="d",
        image="i",
        manifest="x.xml",
        repo_url="https://example.com",
        repo_branch="main",
        kas_container_image="img:latest",
        sstate_dir="/cache/sstate",
        dl_dir="/cache/downloads",
    )


def _install(monkeypatch: pytest.MonkeyPatch, *specs: tuple[str, Severity, Status, str]) -> None:
    checks = []
    names = {}
    severities = {}
    for name, severity, status, message in specs:

        def check(cfg: BuildConfig, _n=name, _s=severity, _st=status, _m=message) -> CheckResult:
            return CheckResult(name=_n, severity=_s, status=_st, message=_m)

        checks.append(check)
        names[check] = name
        severities[name] = severity
    monkeypatch.setattr(diagnostics, "SHARED_CHECKS", tuple(checks))
    monkeypatch.setattr(diagnostics, "_CHECK_NAME", names)
    monkeypatch.setattr(diagnostics, "_CHECK_SEVERITY", severities)
    monkeypatch.setattr(diagnostics, "_DOCKER_CHECKS", ())
    monkeypatch.setattr(diagnostics, "_CLUSTER_CHECKS", ())
    monkeypatch.setattr(diagnostics, "_POST_BUILD_CHECKS", ())


def _gate(tmp_path: Path) -> tuple[list[dict], Path]:
    """Run the gate, return the events and run directory."""
    with RunLogger(tmp_path) as log:
        try:
            _run_doctor_gate(_cfg(), log, bsp=None)
        except typer.Exit as exc:
            assert exc.exit_code == 2
        run_dir = log.run_dir
        events_path = log.events_path
    events = [json.loads(line) for line in events_path.read_text().splitlines() if line.strip()]
    return events, run_dir


def _doctor_fails(events: list[dict]) -> list[dict]:
    return [e for e in events if e["event"] == "step_fail" and e.get("step") == "doctor"]


BLOCK_FAIL = ("disk-space", Severity.BLOCK, Status.FAIL, "only 1 GB free")


def test_blocking_failure_records_check_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, BLOCK_FAIL)
    with RunLogger(tmp_path) as log, pytest.raises(typer.Exit) as exc:
        _run_doctor_gate(_cfg(), log, bsp=None)
    assert exc.value.exit_code == 2
    events = [json.loads(line) for line in log.events_path.read_text().splitlines() if line.strip()]
    fails = _doctor_fails(events)
    assert len(fails) == 1
    assert fails[0]["reason"] == "blocking failure"
    assert fails[0]["error"] == "disk-space: only 1 GB free"


def test_error_names_only_blocking_checks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, BLOCK_FAIL, ("slow-disk", Severity.WARN, Status.FAIL, "spinning rust"))
    events, _ = _gate(tmp_path)
    (fail,) = _doctor_fails(events)
    assert fail["error"] == "disk-space: only 1 GB free"
    assert "slow-disk" not in fail["error"]


def test_warning_only_records_no_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, ("slow-disk", Severity.WARN, Status.FAIL, "spinning rust"))
    events, _ = _gate(tmp_path)
    assert _doctor_fails(events) == []
    assert any(e["event"] == "step_ok" and e.get("step") == "doctor" for e in events)
    assert all("error" not in e for e in events)


def test_hidden_report_records_same_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, BLOCK_FAIL, ("slow-disk", Severity.WARN, Status.FAIL, "spinning rust"))
    monkeypatch.setattr(
        app_state, "_GLOBAL_OPTIONS", dataclasses.replace(app_state._GLOBAL_OPTIONS, hide_doctor_report=True)
    )
    events, _ = _gate(tmp_path)
    (fail,) = _doctor_fails(events)
    assert fail["reason"] == "blocking failure"
    assert fail["error"] == "disk-space: only 1 GB free"


def test_stray_closing_tag_does_not_break_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    message = "[build] x [/y]"
    _install(monkeypatch, ("config", Severity.BLOCK, Status.FAIL, message))
    events, _ = _gate(tmp_path)
    (fail,) = _doctor_fails(events)
    assert fail["reason"] == "blocking failure"
    assert fail["error"] == f"config: {message}"


def test_status_and_triage_report_the_same_text(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    _install(monkeypatch, BLOCK_FAIL)
    (env / "ws" / ".bakar.toml").write_text("")
    runs = env / "ws" / "build" / "runs"
    with RunLogger(runs, run_id=RUN_ID) as log, pytest.raises(typer.Exit):
        _run_doctor_gate(_cfg(), log, bsp=None)
    run = runs / RUN_ID
    with (run / "events.jsonl").open("a") as fh:
        fh.write(json.dumps({"event": "run_end", "exit_code": 2}) + "\n")
    text = "disk-space: only 1 GB free"
    assert _failure(run) == {"recipe": None, "task": None, "step": "doctor", "error": text}
    report = analyse(run, env / "ws")
    assert report.failing_step == "doctor"
    assert report.fail_reason == text


def test_older_record_reads_as_label_and_ignores_diagnosis(env: Path) -> None:  # noqa: F811
    run = _run_dir(env)
    (env / "ws" / ".bakar.toml").write_text("")
    recs = [
        {"event": "step_fail", "step": "doctor", "reason": "blocking failure"},
        {"event": "run_end", "exit_code": 2},
    ]
    (run / "events.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs))
    (run / "diagnosis.txt").write_text("BLOCK FAIL disk-space              only 1 GB free\n")
    failure = _failure(run)
    assert failure is not None
    assert failure["step"] == "doctor"
    assert failure["error"] == "blocking failure"
    report = analyse(run, env / "ws")
    assert report.failing_step == "doctor"
    assert report.fail_reason == "blocking failure"
    assert "disk-space" not in (failure["error"] or "")
    assert "disk-space" not in (report.fail_reason or "")
