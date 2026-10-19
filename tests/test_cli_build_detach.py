"""``bakar build --detach`` and the foreground nudge."""

from __future__ import annotations

from typing import TYPE_CHECKING, NoReturn

import pytest

from bakar.cli import app
from bakar.commands import build as build_cmd

if TYPE_CHECKING:
    from pathlib import Path

    from typer.testing import CliRunner as _CliRunner

pytestmark = pytest.mark.unit


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / ".bakar.toml").write_text("")
    (tmp_path / "nxp").mkdir()
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def generic_yaml(tmp_path: Path) -> Path:
    yaml_path = tmp_path / "my.yml"
    yaml_path.write_text("header:\n  version: 14\nmachine: qemux86-64\n")
    return yaml_path


@pytest.fixture
def launched(monkeypatch: pytest.MonkeyPatch) -> list[tuple[list[str], Path]]:
    calls: list[tuple[list[str], Path]] = []

    def fake(argv, cwd) -> NoReturn:  # type: ignore[no-untyped-def]
        calls.append((list(argv), cwd))
        raise SystemExit(0)

    monkeypatch.setattr(build_cmd._detach_cmd, "detach_and_exit", fake)
    return calls


def _argv(monkeypatch: pytest.MonkeyPatch, *args: str) -> list[str]:
    argv = ["bakar", *args]
    monkeypatch.setattr("sys.argv", argv)
    return argv[1:]


def test_global_options_before_build_reach_the_launcher(
    runner: _CliRunner, launched: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    args = _argv(monkeypatch, "--host", "build", "--detach", "x.yml")
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert launched[0][0] == args


def test_unit_starts_in_the_invoking_cwd(
    runner: _CliRunner, launched: list, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "ws").mkdir()
    monkeypatch.chdir(tmp_path)
    cwd = tmp_path.cwd()
    args = _argv(monkeypatch, "build", "-w", "ws", "--detach", "x.yml")
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert launched[0][1] == cwd


@pytest.mark.parametrize("flags", [["--dry-run"], ["--dry-run-script", "-"]])
def test_detach_with_dry_run_exits_2_unlaunched(
    runner: _CliRunner, launched: list, monkeypatch: pytest.MonkeyPatch, flags: list[str]
) -> None:
    args = _argv(monkeypatch, "build", *flags, "--detach", "x.yml")
    result = runner.invoke(app, args)
    assert result.exit_code == 2
    assert "--detach" in result.output
    assert launched == []


def _nudges(
    runner: _CliRunner, monkeypatch: pytest.MonkeyPatch, workspace: Path, generic_yaml: Path
) -> list[list[str]]:
    emitted: list[list[str]] = []
    monkeypatch.setattr(build_cmd._hints, "emit", lambda lines: emitted.append(list(lines)))
    monkeypatch.setattr(build_cmd.step_kas, "run_build", lambda *a, **k: 0)
    args = _argv(monkeypatch, "build", str(generic_yaml))
    runner.invoke(app, args)
    return emitted


def test_agent_mode_foreground_build_prints_nudge_once(
    runner: _CliRunner, monkeypatch: pytest.MonkeyPatch, workspace: Path, generic_yaml: Path
) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    emitted = _nudges(runner, monkeypatch, workspace, generic_yaml)
    nudge = [line for lines in emitted for line in lines if "--detach" in line]
    assert len(nudge) == 1


def test_no_nudge_without_claudecode(
    runner: _CliRunner, monkeypatch: pytest.MonkeyPatch, workspace: Path, generic_yaml: Path
) -> None:
    monkeypatch.delenv("CLAUDECODE", raising=False)
    emitted = _nudges(runner, monkeypatch, workspace, generic_yaml)
    assert not [line for lines in emitted for line in lines if "--detach" in line]
