"""``_run_doctor_gate`` wires ``run_all``'s ``on_check`` into ``RunLogger``.

Covers task 5.2 (spec: doctor-check-crash-isolation, event-log requirement):
each ``CheckEvent`` emitted by ``run_all`` during a doctor gate becomes a
``check_start``/``check_end`` pair in the run's ``events.jsonl``, bracketed by
the existing ``step_start``/``step_ok`` doctor events, in ``SHARED_CHECKS``
order.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import bakar.diagnostics as diagnostics
from bakar.commands._helpers import _run_doctor_gate
from bakar.config import BuildConfig
from bakar.diagnostics import CheckResult, Severity, Status
from bakar.observability import RunLogger

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
    )  # type: ignore[arg-type]


def _first_check(cfg: BuildConfig) -> CheckResult:
    return CheckResult(name="first-check", severity=Severity.INFO, status=Status.PASS, message="fine")


def _second_check(cfg: BuildConfig) -> CheckResult:
    return CheckResult(name="second-check", severity=Severity.INFO, status=Status.PASS, message="fine")


def _install_two_trivial_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(diagnostics, "SHARED_CHECKS", (_first_check, _second_check))
    monkeypatch.setattr(
        diagnostics,
        "_CHECK_NAME",
        {_first_check: "first-check", _second_check: "second-check"},
    )
    monkeypatch.setattr(
        diagnostics,
        "_CHECK_SEVERITY",
        {"first-check": Severity.INFO, "second-check": Severity.INFO},
    )
    monkeypatch.setattr(diagnostics, "_DOCKER_CHECKS", ())
    monkeypatch.setattr(diagnostics, "_CLUSTER_CHECKS", ())
    monkeypatch.setattr(diagnostics, "_POST_BUILD_CHECKS", ())


def test_doctor_gate_emits_check_events_in_shared_checks_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_two_trivial_checks(monkeypatch)

    with RunLogger(tmp_path) as log:
        _run_doctor_gate(_cfg(), log, bsp=None)
        events_path = log.events_path

    all_records = [json.loads(line) for line in events_path.read_text().splitlines() if line.strip()]
    kinds = [(r["event"], r.get("step") or r.get("check")) for r in all_records]
    doctor_kinds = [
        (event, name) for event, name in kinds if event in {"step_start", "step_ok", "check_start", "check_end"}
    ]

    assert doctor_kinds[0] == ("step_start", "doctor")
    assert doctor_kinds[-1] == ("step_ok", "doctor")

    check_events = doctor_kinds[1:-1]
    assert check_events == [
        ("check_start", "first-check"),
        ("check_end", "first-check"),
        ("check_start", "second-check"),
        ("check_end", "second-check"),
    ]

    # Every check_start has a corresponding check_end later in the file.
    starts = [i for i, (kind, _name) in enumerate(check_events) if kind == "check_start"]
    for i in starts:
        name = check_events[i][1]
        assert any(kind == "check_end" and other_name == name for kind, other_name in check_events[i + 1 :]), (
            f"no check_end found after check_start for {name}"
        )
