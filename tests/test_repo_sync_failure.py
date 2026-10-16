"""A failed ``repo`` call is reported as an error line, not a traceback."""

from __future__ import annotations

import json
import subprocess
import sys
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from bakar.cli import main
from tests.test_cli_sync import _state_needing_sync

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


def test_failed_repo_sync_reports_error_and_records_step_fail(
    fake_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(fake_workspace)
    monkeypatch.setattr(sys, "argv", ["bakar", "sync", "--manifest", "imx-6.6.52-2.2.2.xml"])
    state = _state_needing_sync("nxp")
    monkeypatch.setattr("bakar.commands.sync.detect", lambda cfg: state)

    def _boom(argv: list[str], **kwargs: object) -> None:
        raise subprocess.CalledProcessError(1, argv)

    with (
        patch("bakar.commands._helpers.run_all", return_value=[]),
        patch("bakar.steps.repo.subprocess.run", side_effect=_boom),
    ):
        rc = main()

    err = capsys.readouterr().err
    assert rc == 1
    assert "Error: repo " in err
    assert "exited 1" in err
    assert "Traceback" not in err

    events = [
        json.loads(line) for path in fake_workspace.rglob("events.jsonl") for line in path.read_text().splitlines()
    ]
    fails = [e for e in events if e.get("event") == "step_fail" and e.get("step") == "repo_sync"]
    assert fails, events
    assert fails[0]["returncode"] == 1
