"""Tests for the buildtools step of the interactive ``bakar init`` sync prompt.

Sync, the feature step, the host profile, the plan builder and the runner are
all patched; nothing here touches the host.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
import questionary

from bakar import buildtools
from bakar.commands import _init_checklist, _init_requirements
from bakar.commands import init as init_module
from bakar.commands import sync as sync_module
from bakar.setup import plan as setup_plan
from bakar.setup import runner
from bakar.setup.plan import SetupPlan
from bakar.setup.profile import HostProfile

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit

SYNC = "Run `bakar sync` now?"
INSTALL = "Install the buildtools toolchain?"


class _Q:
    def __init__(self, answer: object) -> None:
        self._answer = answer

    def ask(self) -> object:
        return self._answer


class _Action:
    def __init__(self, check_name: str) -> None:
        self.check_name = check_name

    def describe(self) -> str:
        return f"do {self.check_name}"


def _setup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    host_mode: bool,
    sync_answer: bool,
    toolchain: bool = False,
) -> SimpleNamespace:
    rec = SimpleNamespace(prompts=[], applied=[], builds=0, synced=[], confirmed=[], out=[])
    monkeypatch.setattr(init_module.console, "print", lambda *a, **_k: rec.out.append(" ".join(map(str, a))))
    ws = tmp_path / "ws"

    def confirm(message: str, **_kw: Any) -> _Q:
        rec.prompts.append(message)
        return _Q(sync_answer if message == SYNC else True)

    monkeypatch.setattr(questionary, "confirm", confirm)
    monkeypatch.setattr(questionary, "select", lambda *_a, **_k: _Q("generic"))
    monkeypatch.setattr(questionary, "text", lambda _m, default="", **_k: _Q(default))
    monkeypatch.setattr(questionary, "path", lambda *_a, **_k: _Q(str(ws)))
    monkeypatch.setattr(init_module.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(_init_checklist, "run_feature_step", lambda _ws: None)
    monkeypatch.setattr(
        _init_requirements, "proposed_config", lambda *_a, **_k: (SimpleNamespace(host_mode=host_mode), [])
    )
    monkeypatch.setattr(sync_module, "sync", lambda *, workspace: rec.synced.append(workspace))
    monkeypatch.setattr(HostProfile, "detect", classmethod(lambda cls: object()))
    monkeypatch.setattr(
        buildtools, "detect_buildtools", lambda *_a, **_k: SimpleNamespace(present=toolchain, detail="")
    )

    def build(*_a: Any, **_k: Any) -> SetupPlan:
        rec.builds += 1
        return SetupPlan(actions=[_Action("sysctl"), _Action("host-preflight"), _Action("host-preflight")])

    def apply_plan(plan: SetupPlan, **kw: Any) -> None:
        rec.applied.append([a.check_name for a in plan.actions])
        rec.confirmed.append(kw.get("confirmed"))

    monkeypatch.setattr(setup_plan, "build", build)
    monkeypatch.setattr(runner, "apply_plan", apply_plan)
    return rec


def test_accepted_sync_host_mode_applies_only_host_preflight(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    rec = _setup(monkeypatch, tmp_path, host_mode=True, sync_answer=True)
    init_module._init_interactive(False, None)
    assert rec.synced
    assert rec.applied == [["host-preflight", "host-preflight"]]
    assert rec.confirmed == [True]
    assert rec.prompts.count(INSTALL) == 1
    assert rec.prompts.index(INSTALL) > rec.prompts.index(SYNC)


def test_declined_buildtools_confirmation_applies_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    rec = _setup(monkeypatch, tmp_path, host_mode=True, sync_answer=True)
    monkeypatch.setattr(questionary, "confirm", lambda m, **_k: _Q(m == SYNC))
    init_module._init_interactive(False, None)
    assert rec.applied == []


def test_declined_sync_host_mode_without_toolchain_prints_setup_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rec = _setup(monkeypatch, tmp_path, host_mode=True, sync_answer=False)
    init_module._init_interactive(False, None)
    out = " ".join(" ".join(rec.out).split())
    assert "bakar sync --workspace" in out
    assert "run [bold]bakar setup[/] from the workspace after `bakar sync`" in out
    assert rec.builds == 0
    assert rec.applied == []


def test_declined_sync_host_mode_with_toolchain_prints_no_setup_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rec = _setup(monkeypatch, tmp_path, host_mode=True, sync_answer=False, toolchain=True)
    init_module._init_interactive(False, None)
    assert "bakar setup" not in " ".join(rec.out)


@pytest.mark.parametrize("sync_answer", [True, False])
def test_container_mode_builds_no_plan_and_prints_nothing_new(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, sync_answer: bool
) -> None:
    rec = _setup(monkeypatch, tmp_path, host_mode=False, sync_answer=sync_answer)
    init_module._init_interactive(False, None)
    assert rec.builds == 0
    assert rec.applied == []
    assert INSTALL not in rec.prompts
    assert "bakar setup" not in " ".join(rec.out)
