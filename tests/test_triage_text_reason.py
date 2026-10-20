"""The triage text form prints a recorded failure reason as recorded."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from bakar import observability
from bakar.cli import app
from bakar.commands._app import console

if TYPE_CHECKING:
    from pathlib import Path

    from typer.testing import CliRunner as _CliRunner

pytestmark = pytest.mark.unit

BRACKET_REASON = "kas-yaml-syntax: unknown key [build] in the configuration"
CLOSING_REASON = "bad closing [/build] tag in the reason"
ESCAPED_REASON = r"pre-escaped \[done] tag"


@pytest.fixture(autouse=True)
def _isolated(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("home")))
    monkeypatch.setattr(console, "_width", 400)


def _make_run(tmp_path: Path, reason: str, *, stopped: bool = False) -> Path:
    (tmp_path / ".bakar.toml").write_text("")
    run = tmp_path / "nxp" / "build" / "runs" / "20260529-120000"
    run.mkdir(parents=True)
    rec = {"event": "step_fail", "step": "kas_build", "exit_code": 2, "reason": reason}
    (run / "events.jsonl").write_text(json.dumps(rec) + "\n")
    (run / "kas.log").write_text("NOTE: nothing\n")
    if stopped:
        assert observability.write_stop_request(run)
    return run


def _text(runner: _CliRunner, workspace: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(workspace)
    return runner.invoke(app, ["triage"])


def test_bracketed_word_is_printed(runner: _CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_run(tmp_path, BRACKET_REASON)
    result = _text(runner, tmp_path, monkeypatch)
    assert result.exit_code == 0, result.output
    assert "unknown key [build] in the configuration" in result.output


def test_closing_tag_without_opener_does_not_raise(
    runner: _CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_run(tmp_path, CLOSING_REASON)
    result = _text(runner, tmp_path, monkeypatch)
    assert result.exit_code == 0, result.output
    assert "[/build]" in result.output


def test_pre_escaped_reason_has_no_doubled_backslash(
    runner: _CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_run(tmp_path, ESCAPED_REASON)
    result = _text(runner, tmp_path, monkeypatch)
    assert result.exit_code == 0, result.output
    assert result.output.count("[done]") == 1
    assert "pre-escaped [done] tag" in result.output
    assert "\\" not in result.output


@pytest.mark.parametrize("reason", [BRACKET_REASON, CLOSING_REASON, ESCAPED_REASON])
def test_json_reason_is_byte_for_byte(
    runner: _CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    _make_run(tmp_path, reason)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["triage", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["fail_reason"] == reason


def test_stopped_run_line_is_unchanged(runner: _CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_run(tmp_path, BRACKET_REASON, stopped=True)
    result = _text(runner, tmp_path, monkeypatch)
    assert result.exit_code == 0, result.output
    assert "run was stopped by bakar stop - cancelled on purpose, not a failure to diagnose" in result.output
    assert "unknown key" not in result.output
