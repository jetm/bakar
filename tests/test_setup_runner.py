"""Tests for the ``bakar setup`` runner orchestration.

The bare ``sudo`` / ``subprocess.run`` invocation lines in
:mod:`bakar.setup.runner` are ``# pragma: no cover``; the routing logic around
them is exercised here by monkeypatching ``subprocess.run`` and
``typer.confirm``. Covered:

- declining at the interactive confirm runs no privileged op, no unprivileged
  op, and no config-write ``apply()``;
- ``assume_yes`` with a failing ``sudo -n`` precheck exits non-zero and applies
  nothing, without blocking on a password;
- unprivileged operations run without sudo, and the config-write ``apply()`` is
  invoked last when the run was not declined.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest
import typer

from bakar.setup import runner
from bakar.setup.actions.base import RunCommand, WriteFile
from bakar.setup.plan import SetupPlan

if TYPE_CHECKING:
    from bakar.setup.profile import HostProfile


@dataclass
class _FakeAction:
    """A minimal Action stub returning a fixed operation list."""

    check_name: str
    needs_root: bool
    ops: list[RunCommand | WriteFile] = field(default_factory=list)

    def describe(self) -> str:
        return self.check_name

    def is_satisfied(self, _profile: HostProfile) -> bool:
        return False

    def operations(self) -> list[RunCommand | WriteFile]:
        return self.ops


@dataclass
class _FakeConfigWrite:
    """A config-write stub: no operations, records ``apply()`` calls."""

    check_name: str = "host-config-persist"
    needs_root: bool = False
    applied: int = 0

    def describe(self) -> str:
        return self.check_name

    def is_satisfied(self, _profile: HostProfile) -> bool:
        return False

    def operations(self) -> list[RunCommand | WriteFile]:
        return []

    def apply(self, path=None) -> None:
        self.applied += 1


class _Recorder:
    """Records every ``subprocess.run`` call the runner issues."""

    def __init__(self, *, sudo_n_ok: bool = True) -> None:
        self.calls: list[list[str]] = []
        self._kwargs: list[dict] = []
        self.sudo_n_ok = sudo_n_ok

    def __call__(self, argv, *args, **kwargs):
        self.calls.append(list(argv))
        self._kwargs.append(dict(kwargs))

        class _Result:
            returncode = 0 if (argv[:2] != ["sudo", "-n"] or self.sudo_n_ok) else 1

        return _Result()

    @property
    def sudo_calls(self) -> list[list[str]]:
        return [c for c in self.calls if c and c[0] == "sudo"]

    def kwargs_for(self, argv: list[str]) -> dict:
        """Return kwargs for the first call matching argv."""
        for c, k in zip(self.calls, self._kwargs, strict=False):
            if c == argv:
                return k
        return {}


def test_decline_at_confirm_applies_nothing(monkeypatch) -> None:
    """Declining the interactive confirm runs no op and no config-write apply."""
    recorder = _Recorder()
    monkeypatch.setattr(runner.subprocess, "run", recorder)
    monkeypatch.setattr(typer, "confirm", lambda *a, **k: False)

    priv = _FakeAction("sysctl", needs_root=True, ops=[RunCommand(argv=["sysctl", "--system"], needs_root=True)])
    cfg = _FakeConfigWrite()
    plan = SetupPlan(actions=[priv, cfg])

    runner.apply_plan(plan, assume_yes=False)

    assert recorder.calls == []
    assert cfg.applied == 0


def test_yes_with_failing_sudo_n_exits_nonzero(monkeypatch) -> None:
    """--yes with a failing ``sudo -n`` precheck exits non-zero, applies nothing."""
    recorder = _Recorder(sudo_n_ok=False)
    monkeypatch.setattr(runner.subprocess, "run", recorder)

    priv = _FakeAction("sysctl", needs_root=True, ops=[RunCommand(argv=["sysctl", "--system"], needs_root=True)])
    cfg = _FakeConfigWrite()
    plan = SetupPlan(actions=[priv, cfg])

    with pytest.raises(typer.Exit) as excinfo:
        runner.apply_plan(plan, assume_yes=True)

    assert excinfo.value.exit_code == 1
    # The only subprocess call was the non-blocking ``sudo -n true`` precheck;
    # no privileged script ran and the config-write apply() never fired.
    assert recorder.calls == [["sudo", "-n", "true"]]
    assert cfg.applied == 0


def test_yes_with_passing_sudo_n_runs_script_and_persists(monkeypatch) -> None:
    """--yes with passwordless sudo pipes the script to ``sudo bash -s`` via stdin."""
    recorder = _Recorder(sudo_n_ok=True)
    monkeypatch.setattr(runner.subprocess, "run", recorder)
    rendered = "#!/usr/bin/env bash\nset -euo pipefail\nsysctl --system\n"
    monkeypatch.setattr(runner, "render_script", lambda ops: rendered)

    priv = _FakeAction("sysctl", needs_root=True, ops=[RunCommand(argv=["sysctl", "--system"], needs_root=True)])
    cfg = _FakeConfigWrite()
    plan = SetupPlan(actions=[priv, cfg])

    runner.apply_plan(plan, assume_yes=True)

    # Precheck first, then exactly one sudo bash -s piped via stdin.
    assert recorder.calls[0] == ["sudo", "-n", "true"]
    bash_calls = [c for c in recorder.sudo_calls if c[:3] == ["sudo", "bash", "-s"]]
    assert bash_calls == [["sudo", "bash", "-s"]]
    stdin_kwargs = recorder.kwargs_for(["sudo", "bash", "-s"])
    assert stdin_kwargs.get("input") == rendered
    assert stdin_kwargs.get("text") is True
    assert cfg.applied == 1


def test_unprivileged_ops_run_without_sudo(monkeypatch) -> None:
    """A plan with only unprivileged ops issues no sudo and persists config last."""
    recorder = _Recorder()
    monkeypatch.setattr(runner.subprocess, "run", recorder)

    unpriv = _FakeAction(
        "cache-dirs",
        needs_root=False,
        ops=[RunCommand(argv=["mkdir", "-p", "/home/u/.cache"], needs_root=False)],
    )
    cfg = _FakeConfigWrite()
    plan = SetupPlan(actions=[unpriv, cfg])

    runner.apply_plan(plan, assume_yes=False)

    assert recorder.sudo_calls == []
    assert recorder.calls == [["mkdir", "-p", "/home/u/.cache"]]
    assert cfg.applied == 1


def test_unprivileged_writefile_writes_inline_with_backup(monkeypatch, tmp_path) -> None:
    """An unprivileged WriteFile op writes inline and backs up a pre-existing file."""
    recorder = _Recorder()
    monkeypatch.setattr(runner.subprocess, "run", recorder)
    monkeypatch.setattr(typer, "confirm", lambda *a, **k: True)

    target = tmp_path / "user.conf"
    target.write_text("old\n", encoding="utf-8")
    write_op = WriteFile(path=str(target), content="new", needs_root=False, backup=True)
    action = _FakeAction("git-global-config", needs_root=False, ops=[write_op])
    plan = SetupPlan(actions=[action])

    runner.apply_plan(plan, assume_yes=False)

    assert target.read_text(encoding="utf-8") == "new\n"
    assert (tmp_path / "user.conf.bak").read_text(encoding="utf-8") == "old\n"
    assert recorder.sudo_calls == []


def test_no_privileged_ops_skips_confirm(monkeypatch) -> None:
    """With no privileged ops, no confirm is asked and no sudo precheck runs."""
    recorder = _Recorder()
    monkeypatch.setattr(runner.subprocess, "run", recorder)

    def _fail_confirm(*_a, **_k):
        raise AssertionError("confirm must not be called when no privileged ops exist")

    monkeypatch.setattr(typer, "confirm", _fail_confirm)

    unpriv = _FakeAction(
        "cache-dirs",
        needs_root=False,
        ops=[RunCommand(argv=["mkdir", "-p", "/home/u/.cache"], needs_root=False)],
    )
    plan = SetupPlan(actions=[unpriv])

    runner.apply_plan(plan, assume_yes=True)

    assert recorder.sudo_calls == []
