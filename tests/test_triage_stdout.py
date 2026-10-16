"""Triage keeps human text (report, log tails) on stderr; stdout is for --json only."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from bakar.cli import app
from tests.conftest import SAMPLE_EVENTS_JSONL

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit

_KAS_LOG = "NOTE: start\n[ERROR] linux-imx do_compile failed\nERROR: boom\n"


def _workspace(tmp_path: Path) -> Path:
    (tmp_path / ".bakar.toml").write_text("")
    run = tmp_path / "nxp" / "build" / "runs" / "20260529-120000"
    run.mkdir(parents=True)
    (run / "events.jsonl").write_text(SAMPLE_EVENTS_JSONL)
    (run / "kas.log").write_text(_KAS_LOG)
    return tmp_path


def _runner() -> CliRunner:
    try:
        return CliRunner(mix_stderr=False)  # ty: ignore[unknown-argument]
    except TypeError:
        return CliRunner()


def test_human_report_and_tails_go_to_stderr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(_workspace(tmp_path))
    result = _runner().invoke(app, ["triage"])

    assert result.exit_code == 0, result.stderr
    assert result.stdout == ""
    assert "kas.log (tail):" in result.stderr
    assert "[ERROR] linux-imx do_compile failed" in result.stderr


def test_json_stdout_is_exactly_one_document(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(_workspace(tmp_path))
    result = _runner().invoke(app, ["triage", "--json"])

    assert result.exit_code == 0, result.stderr
    doc = json.loads(result.stdout)
    assert doc["version"] == 1
    assert "[ERROR]" not in result.stdout
