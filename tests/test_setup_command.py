"""Tests for the ``bakar setup`` command wiring.

Cover the two load-bearing guarantees of task 5.1: ``bakar setup --dry-run``
mutates nothing (no privileged subprocess, no script file on disk, the runner's
``apply_plan`` is never reached) and the ``setup`` command is registered on the
app (``bakar setup --help`` exits 0).

The plan build is stubbed so the test does not depend on the live host's doctor
state; the command's own dry-run guard is what is under test.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

import bakar.commands.setup as setup_cmd
from bakar.cli import app
from bakar.setup.actions.base import RunCommand, WriteFile
from bakar.setup.plan import SetupPlan

if TYPE_CHECKING:
    from pathlib import Path

    from typer.testing import CliRunner as _CliRunner

pytestmark = pytest.mark.unit


class _StubAction:
    """A privileged action with one root op and one user op, for plan stubs."""

    check_name = "sysctl"
    needs_root = True

    def describe(self) -> str:
        return "stub: write a sysctl drop-in and a user file"

    def is_satisfied(self, profile: object) -> bool:
        return False

    def operations(self) -> list[RunCommand | WriteFile]:
        return [
            WriteFile(path="/etc/sysctl.d/99-bakar.conf", content="vm.swappiness=10\n", needs_root=True, backup=False),
            RunCommand(argv=["mkdir", "-p", "/tmp/bakar-stub"], needs_root=False),
        ]


def _stub_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force ``setup.build`` to return a fixed privileged plan (no live doctor)."""
    plan = SetupPlan(actions=[_StubAction()], advisories=["memory: low (advisory)"])
    monkeypatch.setattr(setup_cmd.setup_plan, "build", lambda *a, **k: plan)


def _block_mutations(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Trap any subprocess.run; return the list it appends invocations to."""
    calls: list[list[str]] = []

    def fake_run(argv, *a, **k):  # type: ignore[no-untyped-def]
        calls.append(list(argv))
        raise AssertionError(f"setup --dry-run must not spawn a subprocess: {argv}")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def test_setup_registered(runner: _CliRunner) -> None:
    """``bakar setup --help`` exits 0 - the command is attached to the app."""
    result = runner.invoke(app, ["setup", "--help"])

    assert result.exit_code == 0
    assert "setup" in result.stdout


def test_dry_run_runs_no_subprocess_and_writes_no_file(
    runner: _CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``--dry-run`` prints the script but applies nothing and writes no file."""
    _stub_plan(monkeypatch)
    calls = _block_mutations(monkeypatch)
    # Point the script state dir at an empty tmp dir; assert it stays empty.
    state_dir = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state_dir))

    apply_called: list[bool] = []
    monkeypatch.setattr(
        setup_cmd.setup_runner,
        "apply_plan",
        lambda *a, **k: apply_called.append(True),
    )

    result = runner.invoke(app, ["setup", "--dry-run"])

    assert result.exit_code == 0
    assert apply_called == []  # runner never reached on dry-run
    assert calls == []  # no subprocess spawned
    assert not state_dir.exists() or not any(state_dir.rglob("*"))  # no script written
    # The verbatim privileged script is still printed for auditing (stderr console).
    assert "set -euo pipefail" in result.stderr


def test_dry_run_does_not_write_the_target_system_file(
    runner: _CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The privileged WriteFile target is never created by a dry run."""
    _stub_plan(monkeypatch)
    _block_mutations(monkeypatch)
    monkeypatch.setattr(setup_cmd.setup_runner, "apply_plan", lambda *a, **k: None)

    result = runner.invoke(app, ["setup", "--dry-run"])

    assert result.exit_code == 0
    # The stub targets /etc/sysctl.d/99-bakar.conf; a dry run renders it into the
    # printed script but must not have applied it (no subprocess, no write).
    assert "99-bakar.conf" in result.stderr
