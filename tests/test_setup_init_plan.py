"""Tests for the init-facing additions to the setup engine.

- ``build(workspace=...)`` resolves config for that workspace;
- ``build_init_plan`` appends extra actions after the host actions;
- ``apply_plan(confirmed=True)`` skips the confirm prompt for a privileged plan.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from bakar.diagnostics import CheckResult, Severity, Status
from bakar.setup import plan as plan_mod
from bakar.setup import runner
from bakar.setup.actions.base import RunCommand
from bakar.setup.actions.sysctl import SysctlAction
from tests.conftest import make_host_profile

if TYPE_CHECKING:
    from bakar.setup.actions.base import Operation


@dataclass(frozen=True)
class _ResolvedCfg:
    kas_container_image: str = "img:1"
    host_mode: bool = True


@dataclass
class _RootAction:
    needs_root = True
    check_name = "extra-root"

    def describe(self) -> str:
        return "extra root action"

    def is_satisfied(self, _profile: object) -> bool:
        return False

    def operations(self) -> list[Operation]:
        return [RunCommand(argv=["true"], needs_root=True)]


@dataclass
class _ExtraAction:
    check_name = "extra"
    needs_root = False

    def describe(self) -> str:
        return "extra"

    def is_satisfied(self, _profile: object) -> bool:
        return False

    def operations(self) -> list[Operation]:
        return []


def _fail(name: str) -> CheckResult:
    return CheckResult(name=name, severity=Severity.BLOCK, status=Status.FAIL, message=f"{name} failed")


def _patch_resolve(monkeypatch: pytest.MonkeyPatch, captured: dict[str, object]) -> None:
    def _fake_resolve(request):
        captured["workspace"] = request.workspace
        return _ResolvedCfg()

    monkeypatch.setattr(plan_mod.config, "resolve", _fake_resolve)
    monkeypatch.setattr(plan_mod, "run_all", lambda _cfg, _bsp: [_fail("sysctl")])
    monkeypatch.setattr(SysctlAction, "is_satisfied", lambda _self, _p: False)


@pytest.mark.unit
def test_build_resolves_with_explicit_workspace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}
    _patch_resolve(monkeypatch, captured)
    plan_mod.build(make_host_profile(), workspace=tmp_path)
    assert captured["workspace"] == tmp_path


@pytest.mark.unit
def test_build_without_workspace_resolves_cwd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}
    _patch_resolve(monkeypatch, captured)
    monkeypatch.chdir(tmp_path)
    plan_mod.build(make_host_profile())
    assert captured["workspace"] == Path.cwd()


@pytest.mark.unit
def test_init_plan_appends_extra_actions_after_host_actions(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}
    _patch_resolve(monkeypatch, captured)
    extra = _ExtraAction()
    result = plan_mod.build_init_plan(make_host_profile(), workspace=tmp_path, extra_actions=[extra])
    host_only = plan_mod.build(make_host_profile(), workspace=tmp_path)
    assert host_only.actions, "fixture must produce a host action"
    n = len(host_only.actions)
    assert [type(a) for a in result.actions[:n]] == [type(a) for a in host_only.actions]
    assert result.actions[n:] == [extra]
    assert result.advisories == host_only.advisories
    assert captured["workspace"] == tmp_path


@pytest.mark.unit
def test_apply_plan_confirmed_skips_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_a: object, **_k: object) -> bool:
        raise AssertionError("typer.confirm must not be called")

    ran: list[int] = []
    monkeypatch.setattr(runner.typer, "confirm", _boom)
    monkeypatch.setattr(runner, "_run_privileged_script", lambda ops: ran.append(len(ops)))
    runner.apply_plan(plan_mod.SetupPlan(actions=[_RootAction()], advisories=[]), confirmed=True)
    assert ran == [1]


@pytest.mark.unit
def test_apply_plan_default_still_prompts(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[str] = []

    def _decline(msg: str) -> bool:
        asked.append(msg)
        return False

    ran: list[int] = []
    monkeypatch.setattr(runner.typer, "confirm", _decline)
    monkeypatch.setattr(runner, "_run_privileged_script", lambda ops: ran.append(len(ops)))
    runner.apply_plan(plan_mod.SetupPlan(actions=[_RootAction()], advisories=[]))
    assert len(asked) == 1
    assert ran == []
