"""CLI wiring tests for ``bakar build --on <host>`` remote dispatch.

These exercise the ``build()`` command's early ``--on`` interception with the
ssh/rsync subprocess mocked (``bakar.steps.remote_dispatch.subprocess``) and no
live host. The forwarded argv is ``sys.argv[1:]`` (design D2), so the ``--on``
tests monkeypatch ``sys.argv`` to a realistic invocation.

Asserted:
- no ``--on`` spawns neither the ssh nor the rsync mock (local build path);
- an unreachable host exits non-zero with NO rsync call;
- the default remote script carries ``BAKAR_SCCACHE_DIST=0`` while
  ``--sccache-dist`` omits it;
- a non-zero remote exit propagates to the CLI exit code;
- the run-id + ``ssh <host> bakar triage <id>`` line is printed;
- ``--yes`` skips the confirmation prompt, and declining without it aborts.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import pytest

from bakar.cli import app
from bakar.commands import build as build_cmd
from bakar.steps import remote_dispatch as rd

if TYPE_CHECKING:
    from pathlib import Path

    from typer.testing import CliRunner as _CliRunner

pytestmark = pytest.mark.unit

HOST = "pc2"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _stub_doctor_checks():
    """Doctor runs on every build; stub ``run_all`` to all-pass so the local
    build path in the no-``--on`` test stays host-independent."""
    from unittest.mock import patch

    with patch("bakar.commands._helpers.run_all", return_value=[]):
        yield


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A tmp workspace with a ``.bakar.toml`` marker; chdir into it so
    ``_workspace_from_cwd()`` resolves to it."""
    (tmp_path / ".bakar.toml").write_text("")
    (tmp_path / "nxp").mkdir()
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def generic_yaml(tmp_path: Path) -> Path:
    """A minimal generic kas YAML (qemu machine, no NXP/TI markers)."""
    yaml_path = tmp_path / "my.yml"
    yaml_path.write_text("header:\n  version: 14\nmachine: qemux86-64\n")
    return yaml_path


class _FakeStdin:
    def __init__(self) -> None:
        self.buffer = ""

    def write(self, s: str) -> None:
        self.buffer += s

    def close(self) -> None:
        pass


class _FakeProc:
    def __init__(self, lines: list[str], rc: int) -> None:
        self.stdin = _FakeStdin()
        self.stdout = list(lines)
        self._rc = rc

    def wait(self) -> int:
        return self._rc


class _Result:
    def __init__(self, returncode: int = 0, stdout: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


class FakeSubprocess:
    """Records every run/Popen call and dispatches a canned result per argv."""

    PIPE = "PIPE"
    STDOUT = "STDOUT"

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []
        self.reachable_rc = 0
        self.rsync_rc = 0
        self.find_stdout = ""
        self.popen_lines: list[str] = []
        self.popen_rc = 0
        self.last_proc: _FakeProc | None = None

    def run(self, argv, **kwargs) -> _Result:
        argv = list(argv)
        self.calls.append(("run", argv))
        if argv[0] == "ssh" and argv[-1] == "true":
            return _Result(self.reachable_rc)
        if argv[0] == "rsync" and "-n" in argv:
            return _Result(0, stdout="itemized preview line\n")
        if argv[0] == "rsync":
            return _Result(self.rsync_rc)
        if argv[0] == "ssh" and "find" in argv[-1]:
            return _Result(0, stdout=self.find_stdout)
        return _Result(0)

    def Popen(self, argv, **kwargs) -> _FakeProc:  # noqa: N802
        self.calls.append(("Popen", list(argv)))
        self.last_proc = _FakeProc(self.popen_lines, self.popen_rc)
        return self.last_proc


@pytest.fixture
def fake_sp(monkeypatch: pytest.MonkeyPatch) -> FakeSubprocess:
    fake = FakeSubprocess()
    monkeypatch.setattr(rd, "subprocess", fake)
    return fake


# ---------------------------------------------------------------------------
# (a) No --on: neither ssh nor rsync mock is touched (local build path).
# ---------------------------------------------------------------------------


def test_no_on_option_does_not_dispatch(
    runner: _CliRunner,
    workspace: Path,
    generic_yaml: Path,
    fake_sp: FakeSubprocess,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(build_cmd.step_kas, "run_build", lambda ctx, **kw: 0)

    result = runner.invoke(app, ["build", str(generic_yaml)])

    assert result.exit_code == 0, result.output
    # The remote dispatch subprocess was never invoked: no ssh, no rsync.
    assert fake_sp.calls == []


# ---------------------------------------------------------------------------
# (b) Unreachable host: exit non-zero, NO rsync spawned.
# ---------------------------------------------------------------------------


def test_unreachable_host_exits_nonzero_no_rsync(
    runner: _CliRunner,
    workspace: Path,
    generic_yaml: Path,
    fake_sp: FakeSubprocess,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["bakar", "build", str(generic_yaml), "--on", HOST])
    fake_sp.reachable_rc = 255

    result = runner.invoke(app, ["build", str(generic_yaml), "--on", HOST])

    assert result.exit_code != 0
    assert not any(argv[0] == "rsync" for _, argv in fake_sp.calls)
    assert not any(kind == "Popen" for kind, _ in fake_sp.calls)


# ---------------------------------------------------------------------------
# (c) sccache-dist off by default; --sccache-dist opts back in.
# ---------------------------------------------------------------------------


def test_default_remote_script_carries_sccache_off(
    runner: _CliRunner,
    workspace: Path,
    generic_yaml: Path,
    fake_sp: FakeSubprocess,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["bakar", "build", str(generic_yaml), "--on", HOST, "--yes"])
    fake_sp.find_stdout = f"1.0 {workspace}/build/runs/20260716-000000\n"

    result = runner.invoke(app, ["build", str(generic_yaml), "--on", HOST, "--yes"])

    assert result.exit_code == 0, result.output
    assert fake_sp.last_proc is not None
    assert "BAKAR_SCCACHE_DIST=0" in fake_sp.last_proc.stdin.buffer


def test_sccache_dist_optin_omits_env_token(
    runner: _CliRunner,
    workspace: Path,
    generic_yaml: Path,
    fake_sp: FakeSubprocess,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["bakar", "--sccache-dist", "build", str(generic_yaml), "--on", HOST, "--yes"])
    fake_sp.find_stdout = f"1.0 {workspace}/build/runs/20260716-000000\n"

    result = runner.invoke(app, ["--sccache-dist", "build", str(generic_yaml), "--on", HOST, "--yes"])

    assert result.exit_code == 0, result.output
    assert fake_sp.last_proc is not None
    assert "BAKAR_SCCACHE_DIST=0" not in fake_sp.last_proc.stdin.buffer


# ---------------------------------------------------------------------------
# (d) Non-zero remote exit propagates to the CLI exit code.
# ---------------------------------------------------------------------------


def test_remote_exit_code_propagates(
    runner: _CliRunner,
    workspace: Path,
    generic_yaml: Path,
    fake_sp: FakeSubprocess,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["bakar", "build", str(generic_yaml), "--on", HOST, "--yes"])
    fake_sp.popen_rc = 7
    fake_sp.popen_lines = ["Run `bakar triage 20260716-120000` for details.\n"]

    result = runner.invoke(app, ["build", str(generic_yaml), "--on", HOST, "--yes"])

    assert result.exit_code == 7


# ---------------------------------------------------------------------------
# (e) run-id + triage command line surfaced.
# ---------------------------------------------------------------------------


def test_run_id_and_triage_line_printed(
    runner: _CliRunner,
    workspace: Path,
    generic_yaml: Path,
    fake_sp: FakeSubprocess,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["bakar", "build", str(generic_yaml), "--on", HOST, "--yes"])
    fake_sp.popen_rc = 1
    fake_sp.popen_lines = ["some output\n", "Run `bakar triage 20260716-120000` for details.\n"]

    result = runner.invoke(app, ["build", str(generic_yaml), "--on", HOST, "--yes"])

    assert "20260716-120000" in result.output
    assert f"ssh {HOST} bakar triage 20260716-120000" in result.output


# ---------------------------------------------------------------------------
# (f) --yes skips the confirmation prompt; declining without it aborts.
# ---------------------------------------------------------------------------


def test_yes_skips_confirmation_prompt(
    runner: _CliRunner,
    workspace: Path,
    generic_yaml: Path,
    fake_sp: FakeSubprocess,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*a, **k):
        raise AssertionError("typer.confirm must not be called with --yes")

    monkeypatch.setattr(rd.typer, "confirm", _boom)
    monkeypatch.setattr(sys, "argv", ["bakar", "build", str(generic_yaml), "--on", HOST, "--yes"])
    fake_sp.find_stdout = f"1.0 {workspace}/build/runs/20260716-000000\n"

    result = runner.invoke(app, ["build", str(generic_yaml), "--on", HOST, "--yes"])

    assert result.exit_code == 0, result.output


def test_without_yes_declined_confirm_aborts(
    runner: _CliRunner,
    workspace: Path,
    generic_yaml: Path,
    fake_sp: FakeSubprocess,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rd.typer, "confirm", lambda *a, **k: False)
    monkeypatch.setattr(sys, "argv", ["bakar", "build", str(generic_yaml), "--on", HOST])

    result = runner.invoke(app, ["build", str(generic_yaml), "--on", HOST])

    assert result.exit_code != 0
    # A dry-run preview may run inside confirm, but the real rsync must not.
    assert not any(argv[0] == "rsync" and "-n" not in argv for _, argv in fake_sp.calls)
    assert not any(kind == "Popen" for kind, _ in fake_sp.calls)
