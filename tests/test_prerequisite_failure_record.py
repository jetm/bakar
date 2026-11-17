"""A host prerequisite failure leaves a run record naming its cause.

Drives ``bakar.cli.main`` on a host build whose workspace lacks the bundled
bitbake bin directory, with the real run logger, then reads the run directory
through the triage and status readers. The recorded cause must reach both, and
the exit status and printed line must stay what they were before the record
gained the cause.
"""

from __future__ import annotations

import re
import sys
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

import bakar.commands.status as status_cmd
from bakar.cli import main
from bakar.steps import kas_build
from bakar.triage import analyse

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def test_missing_bitbake_bin_is_named_in_triage_status_and_console(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / ".bakar.toml").write_text("")
    kas_yaml = workspace / "my.yml"
    kas_yaml.write_text("header:\n  version: 14\nmachine: qemux86-64\n")
    missing_bin = workspace / "bitbake" / "bin"
    assert not missing_bin.exists()

    monkeypatch.chdir(workspace)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("COLUMNS", "500")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.delenv("BAKAR_CONTAINER", raising=False)
    monkeypatch.setenv("BAKAR_HOST_MODE", "1")
    monkeypatch.setattr(sys, "argv", ["bakar", "build", str(kas_yaml)])
    # The toolchain step is not what this test is about; the bin check follows it.
    monkeypatch.setattr(kas_build, "_provision_buildtools", lambda cfg, passthrough: None)

    with patch("bakar.commands._helpers.run_all", return_value=[]):
        rc = main()

    out = capsys.readouterr()
    console_text = _squash(out.out + out.err)
    assert rc == 1
    assert (
        f"Error: host build requires the bundled bitbake bin directory on the launch PATH, "
        f"but it does not exist: {missing_bin}. "
    ) in console_text

    event_files = sorted(workspace.rglob("events.jsonl"))
    assert len(event_files) == 1, event_files
    run_dir = event_files[0].parent

    report = analyse(run_dir, workspace)
    assert report.fail_reason is not None
    assert str(missing_bin) in report.fail_reason
    assert "wrapper-crash" not in report.fail_reason

    monkeypatch.setattr(status_cmd, "_unit_active", lambda _u: False)
    monkeypatch.setattr(status_cmd, "is_build_running", lambda _rd: (False, None, False))
    monkeypatch.setattr(status_cmd, "_frontend_alive", lambda _rd: False)
    rec = status_cmd.compute_status(run_dir.name)
    assert rec is not None
    assert rec.state == "failed"
    assert rec.failure is not None
    assert rec.failure["error"] is not None
    assert str(missing_bin) in rec.failure["error"]
    assert "wrapper-crash" not in rec.failure["error"]
