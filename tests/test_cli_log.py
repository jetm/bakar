"""Tests for the ``bakar log`` command.

The handler tails a run-log file by spinning on ``_tail_follow``, which
enters an unbounded ``while True`` reading new bytes from the file
(``src/bakar/commands/log.py:41-47``). A CliRunner invocation would
block there, so each happy-path test patches
``bakar.commands.log._tail_follow`` with a stand-in that prints the
file contents once and returns. That keeps the test exercising the
real argument parsing, dispatch, run-directory resolution, file
selection, and fallback paths while sidestepping the blocking tail.

Workspace shape: tests build ``<tmp_path>/nxp/build/runs/<run-id>/``
directly (the shared ``fake_run_dir`` fixture writes under
``<tmp_path>/build/runs/`` which doesn't match the NXP family's
``bsp_root = workspace/nxp`` layout). Run-log content is sourced from
the shared ``SAMPLE_EVENTS_JSONL`` and ``SAMPLE_KAS_LOG`` constants in
``tests/conftest.py`` so all run-dir tests share one synthetic shape.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

import bakar.commands.log as log_module
from bakar.commands import app
from tests.conftest import SAMPLE_EVENTS_JSONL, SAMPLE_KAS_LOG

if TYPE_CHECKING:
    from pathlib import Path

    from typer.testing import CliRunner

pytestmark = pytest.mark.unit

_RUN_ID = "20260529-120000"


@pytest.fixture
def nxp_workspace_with_run(tmp_path: Path) -> Path:
    """NXP workspace with a single synthetic run dir at the NXP layout.

    Layout: ``<tmp_path>/nxp/build/runs/<_RUN_ID>/{events.jsonl, kas.log}``.
    Workspace detection picks NXP via the ``nxp/`` subdir; the run dir
    sits where ``cfg.runs_dir`` (= ``workspace/nxp/build/runs``) expects
    it.
    """
    run = tmp_path / "nxp" / "build" / "runs" / _RUN_ID
    run.mkdir(parents=True)
    (run / "events.jsonl").write_text(SAMPLE_EVENTS_JSONL)
    (run / "kas.log").write_text(SAMPLE_KAS_LOG)
    return tmp_path


def _stub_tail_follow(path, history_lines: int = 40) -> None:
    """Non-blocking stand-in for ``_tail_follow``.

    The real helper enters ``while True: time.sleep(0.2)`` after
    flushing the file head, which would hang CliRunner. The stub
    prints the file's content to stdout once and returns so the
    command can complete normally.
    """
    import sys

    sys.stdout.write(path.read_text())
    sys.stdout.flush()


def test_tail_kas_log(
    runner: CliRunner,
    nxp_workspace_with_run: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--which kas`` (the default) prints kas.log content and exits 0."""
    monkeypatch.setattr(log_module, "_tail_follow", _stub_tail_follow)

    result = runner.invoke(app, ["log", "--workspace", str(nxp_workspace_with_run)])

    assert result.exit_code == 0, result.stderr
    # SAMPLE_KAS_LOG contains the recipe-error line; assert a verbatim
    # fragment so swapping kas.log for events.jsonl would fail the test.
    assert "linux-imx" in result.stdout
    assert "do_compile" in result.stdout


def test_tail_events_jsonl(
    runner: CliRunner,
    nxp_workspace_with_run: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--which events`` prints events.jsonl content (not kas.log)."""
    monkeypatch.setattr(log_module, "_tail_follow", _stub_tail_follow)

    result = runner.invoke(
        app,
        ["log", "--which", "events", "--workspace", str(nxp_workspace_with_run)],
    )

    assert result.exit_code == 0, result.stderr
    # step_fail is the JSON event token unique to events.jsonl; the
    # recipe-error string from SAMPLE_KAS_LOG must NOT appear.
    assert "step_fail" in result.stdout
    assert "kas-build" in result.stdout
    assert "do_compile" not in result.stdout


def test_tail_console_log_absent_falls_back_or_errors(
    runner: CliRunner,
    nxp_workspace_with_run: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--which console`` exits non-zero when console.log is absent.

    The fallback chain in ``log.py`` is asymmetric: a missing
    ``kas.log`` falls back to ``console.log`` (lines 122-129), but a
    missing ``console.log`` or ``events.jsonl`` has no fallback and
    exits 1 (line 133-135). console.log is never created by the
    fixture, so this exercises that bare-error branch.
    """
    monkeypatch.setattr(log_module, "_tail_follow", _stub_tail_follow)

    result = runner.invoke(
        app,
        ["log", "--which", "console", "--workspace", str(nxp_workspace_with_run)],
    )

    assert result.exit_code != 0
    assert "log file not found" in result.stderr


def test_kas_log_missing_falls_back_to_console_log(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing kas.log silently falls back to console.log (lines 122-129).

    Builds a run dir with only console.log present, then asks for the
    default ``--which kas``. The handler must announce the fallback and
    serve console.log's content.
    """
    monkeypatch.setattr(log_module, "_tail_follow", _stub_tail_follow)
    run = tmp_path / "nxp" / "build" / "runs" / _RUN_ID
    run.mkdir(parents=True)
    (run / "console.log").write_text("console-only sentinel line\n")

    result = runner.invoke(app, ["log", "--workspace", str(tmp_path)])

    assert result.exit_code == 0, result.stderr
    assert "console-only sentinel line" in result.stdout
    # The fallback emits a [yellow]note:[/] line on the console (stderr).
    assert "falling back" in result.stderr


def test_unknown_which_exits_2(runner: CliRunner, nxp_workspace_with_run: Path) -> None:
    """Unknown ``--which`` value exits 2 with a helpful message.

    Validated before any dispatch (lines 82-84) so this test does not
    need ``_tail_follow`` patched.
    """
    result = runner.invoke(
        app,
        ["log", "--which", "bogus", "--workspace", str(nxp_workspace_with_run)],
    )

    assert result.exit_code == 2
    assert "invalid --which value" in result.stderr
    assert "bogus" in result.stderr


def test_no_runs_dir_exits_nonzero(runner: CliRunner, tmp_path: Path) -> None:
    """Workspace without any ``build/runs/`` exits 1 with the 'no runs' hint."""
    # Bare workspace: nxp/ exists so detection picks NXP, but no build/runs.
    (tmp_path / "nxp").mkdir()

    result = runner.invoke(app, ["log", "--workspace", str(tmp_path)])

    assert result.exit_code == 1
    assert "no runs yet" in result.stderr


def test_explicit_run_selects_older_run(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--run <id>`` selects the named run dir, not the latest one.

    Creates two run dirs (an older one and a newer one). With no
    ``--run`` the handler picks the lexicographically-last entry
    (line 112: ``run_dirs[-1]``); passing the older ID must surface
    its content instead.
    """
    monkeypatch.setattr(log_module, "_tail_follow", _stub_tail_follow)
    runs = tmp_path / "nxp" / "build" / "runs"
    older = runs / "20260101-000000"
    newer = runs / "20260601-120000"
    older.mkdir(parents=True)
    newer.mkdir(parents=True)
    (older / "kas.log").write_text("older-run sentinel\n")
    (newer / "kas.log").write_text("newer-run sentinel\n")

    result = runner.invoke(
        app,
        ["log", "--run", "20260101-000000", "--workspace", str(tmp_path)],
    )

    assert result.exit_code == 0, result.stderr
    assert "older-run sentinel" in result.stdout
    assert "newer-run sentinel" not in result.stdout


def test_explicit_run_not_found_exits_nonzero(
    runner: CliRunner,
    nxp_workspace_with_run: Path,
) -> None:
    """``--run <id>`` for a non-existent run id exits 1 with a helpful message."""
    result = runner.invoke(
        app,
        ["log", "--run", "99991231-235959", "--workspace", str(nxp_workspace_with_run)],
    )

    assert result.exit_code == 1
    assert "run directory not found" in result.stderr
