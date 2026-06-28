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

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import typer

from bakar.diagnostics import BuildtoolsToolchain
from bakar.setup import runner
from bakar.setup.actions.base import RunCommand, WriteFile
from bakar.setup.actions.tools import BuildtoolsConfigPersistAction, BuildtoolsInstallAction
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


# ---------------------------------------------------------------------------
# Failure-surfacing for buildtools provisioning
#
# A failed or partial install must NOT leave a dead ``[build] buildtools_dir``
# in the global config. Two failure modes:
#   (a) ``install-buildtools`` exits non-zero  -> the runner's ``check=True``
#       raises, aborting the plan before the persist action's ``apply()`` runs.
#   (b) ``install-buildtools`` exits 0 but the toolchain is still undetectable
#       -> the persist action re-checks ``detect_buildtools`` and writes nothing.
# ---------------------------------------------------------------------------


def _install_argv() -> list[str]:
    """The argv the install action runs, naming buildtools-extended via the script."""
    return ["/ws/openembedded-core/scripts/install-buildtools", "-d", "/opt/bakar/buildtools"]


def test_failed_install_surfaces_and_leaves_buildtools_dir_unset(monkeypatch) -> None:
    """A non-zero install exit raises (naming buildtools) and never persists the dir."""
    install_argv = _install_argv()

    def _run(argv, *_a, **_k):
        if argv == install_argv:
            raise subprocess.CalledProcessError(returncode=1, cmd=argv)

        class _Result:
            returncode = 0

        return _Result()

    monkeypatch.setattr(runner.subprocess, "run", _run)

    persisted: list[tuple[str, str, Path | None]] = []
    monkeypatch.setattr(
        "bakar.setup.actions.tools.set_setting",
        lambda key, value, path=None: persisted.append((key, value, path)),
    )
    # Even if the guard were reached, detection reports absent.
    monkeypatch.setattr(
        "bakar.setup.actions.tools.detect_buildtools",
        lambda: BuildtoolsToolchain(present=False, detail="absent"),
    )

    install = BuildtoolsInstallAction(
        install_buildtools=install_argv[0],
        install_dir=Path(install_argv[2]),
    )
    persist = BuildtoolsConfigPersistAction(install_dir=Path(install_argv[2]))
    plan = SetupPlan(actions=[install, persist])

    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        runner.apply_plan(plan, assume_yes=True)

    # The failure names the buildtools-extended installer, so the user sees what broke.
    assert "buildtools" in " ".join(excinfo.value.cmd)
    # The plan aborted before persistence: no dead config path recorded.
    assert persisted == []


def test_install_exit_zero_but_still_absent_persists_nothing(monkeypatch) -> None:
    """An install that exits 0 yet leaves the toolchain undetectable writes no config."""
    install_argv = _install_argv()
    recorder = _Recorder()
    monkeypatch.setattr(runner.subprocess, "run", recorder)

    persisted: list[tuple[str, str, Path | None]] = []
    monkeypatch.setattr(
        "bakar.setup.actions.tools.set_setting",
        lambda key, value, path=None: persisted.append((key, value, path)),
    )
    # Install ran cleanly, but detection still cannot find a toolchain.
    monkeypatch.setattr(
        "bakar.setup.actions.tools.detect_buildtools",
        lambda: BuildtoolsToolchain(present=False, detail="absent"),
    )

    install = BuildtoolsInstallAction(
        install_buildtools=install_argv[0],
        install_dir=Path(install_argv[2]),
    )
    persist = BuildtoolsConfigPersistAction(install_dir=Path(install_argv[2]))
    plan = SetupPlan(actions=[install, persist])

    runner.apply_plan(plan, assume_yes=True)

    # The install op ran; the persist guard declined to record a dead path.
    assert recorder.calls == [install_argv]
    assert persisted == []
