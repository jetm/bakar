"""Tests for the ``bakar prefetch`` command.

Drives the command through the Typer ``CliRunner``, monkeypatching
``run_shell`` so no real kas/kas-container invocation happens. The prefetch
command calls ``step_kas.run_shell(...)`` where ``step_kas`` is the imported
``bakar.steps.kas_build`` module, so the stub is installed on
``prefetch_module.step_kas`` (the attribute the command actually looks up).

The stub captures the ``command=`` kwarg and the resolved ``cfg`` so the tests
can assert on the fetch command string and the resolved machine.

The prefetch console may write to stderr; CliRunner mixes both streams into
``result.output`` (asserting against ``result.stdout`` would miss stderr).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

import bakar.commands.prefetch as prefetch_module
from bakar.cli import app

if TYPE_CHECKING:
    from pathlib import Path

    from typer.testing import CliRunner as _CliRunner

pytestmark = pytest.mark.unit


@pytest.fixture
def runner() -> _CliRunner:
    from typer.testing import CliRunner

    return CliRunner()


@pytest.fixture
def nxp_workspace(tmp_path: Path) -> Path:
    """A workspace with an ``nxp/`` subdir so workspace detection picks nxp."""
    (tmp_path / "nxp").mkdir()
    return tmp_path


class _ShellStub:
    """Records the ``cfg`` and ``command=`` from each ``run_shell`` call."""

    def __init__(self, rc: int = 0) -> None:
        self.rc = rc
        self.cfg = None
        self.command: str | None = None
        self.called = False

    def __call__(self, ctx, args, *, command=None) -> int:
        self.called = True
        self.cfg = ctx.cfg
        self.command = command
        return self.rc


@pytest.mark.unit
def test_prefetch_invokes_runall_fetch_with_image(
    runner: _CliRunner, nxp_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """prefetch calls run_shell with a ``bitbake --runall=fetch <image>`` command."""
    stub = _ShellStub(rc=0)
    monkeypatch.setattr(prefetch_module.step_kas, "run_shell", stub)
    result = runner.invoke(app, ["prefetch", "--workspace", str(nxp_workspace)])
    assert result.exit_code == 0, result.output
    assert stub.called
    assert stub.command is not None
    assert "bitbake --runall=fetch" in stub.command
    # The resolved image must appear in the fetch command string.
    assert stub.cfg.image in stub.command


@pytest.mark.unit
def test_prefetch_machine_override_reaches_invocation(
    runner: _CliRunner, nxp_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``-m imx95-var-dart`` makes the resolved cfg.machine reach run_shell."""
    stub = _ShellStub(rc=0)
    monkeypatch.setattr(prefetch_module.step_kas, "run_shell", stub)
    result = runner.invoke(
        app,
        ["prefetch", "-m", "imx95-var-dart", "--workspace", str(nxp_workspace)],
    )
    assert result.exit_code == 0, result.output
    assert stub.cfg is not None
    assert stub.cfg.machine == "imx95-var-dart"


@pytest.mark.unit
def test_prefetch_nonzero_return_propagates(
    runner: _CliRunner, nxp_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-zero run_shell return makes the command exit non-zero."""
    stub = _ShellStub(rc=3)
    monkeypatch.setattr(prefetch_module.step_kas, "run_shell", stub)
    result = runner.invoke(app, ["prefetch", "--workspace", str(nxp_workspace)])
    assert result.exit_code != 0, result.output
