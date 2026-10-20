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

import os
from typing import TYPE_CHECKING

import pytest

import bakar.commands.log as log_module
from bakar import detach_records
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


@pytest.fixture
def registered_run_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Chdir outside any workspace and point ``XDG_RUNTIME_DIR`` at a private dir.

    Returns the runtime directory; register runs into it with
    ``detach_records.append_registration``.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    runtime.chmod(0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.chdir(outside)
    return runtime


def _make_run_dir(base: Path, name: str, files: dict[str, str]) -> Path:
    run_dir = base / name
    run_dir.mkdir(parents=True)
    for fname, text in files.items():
        (run_dir / fname).write_text(text)
    return run_dir


def _record_tail(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Replace ``_tail_follow`` with a recorder of the paths it is handed."""
    followed: list[Path] = []

    def _fake(path: Path, history_lines: int = 40) -> None:
        followed.append(path)

    monkeypatch.setattr(log_module, "_tail_follow", _fake)
    return followed


def test_registered_run_followed_outside_workspace(
    runner: CliRunner,
    registered_run_env: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--run <id>`` outside a workspace follows the registered run's kas.log."""
    followed = _record_tail(monkeypatch)
    run_dir = _make_run_dir(tmp_path / "ws" / "build" / "runs", _RUN_ID, {"kas.log": "x\n"})
    assert detach_records.append_registration(detach_records.new_unit_name(), _RUN_ID, run_dir, 1)

    result = runner.invoke(app, ["log", "--run", _RUN_ID])

    assert result.exit_code == 0, result.stderr
    assert followed == [run_dir / "kas.log"]
    assert "Not inside" not in result.stderr


def test_registered_run_newest_unit_without_run(
    runner: CliRunner,
    registered_run_env: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without ``--run`` the newest unit's latest registered run is followed."""
    followed = _record_tail(monkeypatch)
    runs = tmp_path / "ws" / "build" / "runs"
    old_dir = _make_run_dir(runs, "20260101-000000", {"kas.log": "old\n"})
    first_dir = _make_run_dir(runs, "20260201-000000", {"kas.log": "first\n"})
    latest_dir = _make_run_dir(runs, "20260301-000000", {"kas.log": "latest\n"})
    old_unit, new_unit = detach_records.new_unit_name(), detach_records.new_unit_name()
    assert detach_records.append_registration(old_unit, old_dir.name, old_dir, 1)
    assert detach_records.append_registration(new_unit, first_dir.name, first_dir, 2)
    assert detach_records.append_registration(new_unit, latest_dir.name, latest_dir, 2)
    os.utime(registered_run_env / f"{old_unit}.runs", (2_000_000, 2_000_000))
    os.utime(registered_run_env / f"{new_unit}.runs", (3_000_000, 3_000_000))

    result = runner.invoke(app, ["log"])

    assert result.exit_code == 0, result.stderr
    assert followed == [latest_dir / "kas.log"]


@pytest.mark.parametrize(("which", "fname"), [("console", "console.log"), ("events", "events.jsonl")])
def test_registered_run_which_selects_file(
    runner: CliRunner,
    registered_run_env: Path,
    monkeypatch: pytest.MonkeyPatch,
    which: str,
    fname: str,
) -> None:
    """``--which console`` / ``--which events`` pick that file of the registered run."""
    followed = _record_tail(monkeypatch)
    run_dir = _make_run_dir(
        registered_run_env.parent / "ws" / "build" / "runs",
        _RUN_ID,
        {"kas.log": "k\n", "console.log": "c\n", "events.jsonl": SAMPLE_EVENTS_JSONL},
    )
    assert detach_records.append_registration(detach_records.new_unit_name(), _RUN_ID, run_dir, 1)

    result = runner.invoke(app, ["log", "--which", which, "--run", _RUN_ID])

    assert result.exit_code == 0, result.stderr
    assert followed == [run_dir / fname]


def _forbid_registry(monkeypatch: pytest.MonkeyPatch, reason: str) -> None:
    def _boom(*_a: object) -> None:
        raise AssertionError(reason)

    monkeypatch.setattr(detach_records, "run_dir_for_run", _boom)
    monkeypatch.setattr(detach_records, "newest_run_dir", _boom)


def test_registered_run_not_consulted_inside_workspace(
    runner: CliRunner,
    nxp_workspace_with_run: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inside a workspace (cwd or ``-w``) the registry is never read."""
    _forbid_registry(monkeypatch, "registry consulted inside a workspace")
    followed = _record_tail(monkeypatch)
    expected = nxp_workspace_with_run / "nxp" / "build" / "runs" / _RUN_ID / "kas.log"

    monkeypatch.chdir(nxp_workspace_with_run)
    for args in (["log"], ["log", "--run", _RUN_ID], ["log", "--workspace", str(nxp_workspace_with_run)]):
        result = runner.invoke(app, args)
        assert result.exit_code == 0, result.stderr

    assert [p.resolve() for p in followed] == [expected.resolve()] * 3


def test_registered_run_unknown_id_and_bad_which(
    runner: CliRunner,
    registered_run_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unregistered ``--run`` outside a workspace fails as before; bad ``--which`` exits 2 first."""
    followed = _record_tail(monkeypatch)
    result = runner.invoke(app, ["log", "--run", "99991231-235959"])
    # Unchanged pre-registry behaviour: the workspace error, exit 2, no traceback.
    assert result.exit_code == 2
    assert "Not inside a BSP workspace" in result.stderr
    assert isinstance(result.exception, SystemExit)
    assert followed == []

    _forbid_registry(monkeypatch, "looked up before --which validation")

    def _no_cwd_lookup() -> None:
        raise AssertionError("workspace lookup before --which validation")

    monkeypatch.setattr(log_module, "_find_workspace_from_cwd", _no_cwd_lookup)
    result = runner.invoke(app, ["log", "--which", "bogus", "--run", _RUN_ID])
    assert result.exit_code == 2
    assert "invalid --which value" in result.stderr


def _record_stream(monkeypatch: pytest.MonkeyPatch, result: int | BaseException = 0) -> list[tuple]:
    """Replace ``stream_on_host`` with a recorder that returns ``result`` (or raises it)."""
    import bakar.commands.status as status_module

    calls: list[tuple] = []

    def _fake(host: str, command: str, options: list[str], *, transport_exit: int) -> int:
        calls.append((host, command, list(options), transport_exit))
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(status_module, "stream_on_host", _fake)
    return calls


def test_on_host_forwards_default_options(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    """``bakar log --on pc3`` streams ``bakar log --which kas`` from pc3 with transport_exit=2."""
    calls = _record_stream(monkeypatch)

    result = runner.invoke(app, ["log", "--on", "pc3"])

    assert result.exit_code == 0, result.stderr
    assert calls == [("pc3", "log", ["--which", "kas"], 2)]


def test_on_host_forwards_run_and_which(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _record_stream(monkeypatch)

    result = runner.invoke(app, ["log", "--on", "pc3", "--run", "R", "--which", "console"])

    assert result.exit_code == 0, result.stderr
    assert calls == [("pc3", "log", ["--which", "console", "--run", "R"], 2)]


@pytest.mark.parametrize(
    ("extra", "label"),
    [
        (["my.yml"], "the positional kas YAML"),
        # -w must name an existing directory or Typer rejects it before log_cmd runs.
        (["-w", "{tmp}"], "-w/--workspace"),
        (["-f", "default.xml"], "-f/--manifest"),
    ],
)
def test_on_host_refuses_local_paths(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extra: list[str], label: str
) -> None:
    calls = _record_stream(monkeypatch)
    args = [a.replace("{tmp}", str(tmp_path)) for a in extra]

    result = runner.invoke(app, ["log", *args, "--on", "pc3"])

    assert result.exit_code == 2
    assert f"bakar log: {label} names a local path and cannot be combined with --on" in result.stderr
    assert calls == []


@pytest.mark.parametrize("code", [0, 1, 2, 7])
def test_on_host_exit_status_is_stream_result(runner: CliRunner, monkeypatch: pytest.MonkeyPatch, code: int) -> None:
    _record_stream(monkeypatch, code)

    result = runner.invoke(app, ["log", "--on", "pc3"])

    assert result.exit_code == code


def test_on_host_keyboard_interrupt_exits_0(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _record_stream(monkeypatch, KeyboardInterrupt())

    result = runner.invoke(app, ["log", "--on", "pc3", "--run", "R"])

    assert result.exit_code == 0
    assert len(calls) == 1


def test_on_host_resolves_nothing_locally(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    """No workspace, config or registry lookup happens locally with ``--on``."""
    calls = _record_stream(monkeypatch)

    def _boom(*_a: object, **_k: object) -> None:
        raise AssertionError("local resolution with --on")

    monkeypatch.setattr(log_module, "_resolve_workspace", _boom)
    monkeypatch.setattr(log_module, "_find_workspace_from_cwd", _boom)
    monkeypatch.setattr(log_module, "resolve", _boom)
    _forbid_registry(monkeypatch, "registry read locally with --on")

    result = runner.invoke(app, ["log", "--on", "pc3", "--run", "R"])

    assert result.exit_code == 0, result.stderr
    assert len(calls) == 1


def test_on_host_invalid_which_exits_2_first(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _record_stream(monkeypatch)

    result = runner.invoke(app, ["log", "--on", "pc3", "--which", "bogus"])

    assert result.exit_code == 2
    assert "invalid --which value" in result.stderr
    assert calls == []


def _numbered_log(run_dir: Path, count: int, name: str = "kas.log", pad: int = 0) -> None:
    (run_dir / name).write_text("".join(f"line {i:07d}{'x' * pad}\n" for i in range(1, count + 1)))


def test_no_follow_prints_exactly_last_n_lines(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = _make_run_dir(tmp_path, "r1", {})
    _numbered_log(run_dir, 100)
    followed = _record_tail(monkeypatch)
    monkeypatch.setattr(detach_records, "run_dir_for_run", lambda _r: run_dir)
    monkeypatch.setattr(log_module, "_find_workspace_from_cwd", lambda: None)

    result = runner.invoke(app, ["log", "--run", "r1", "--lines", "5", "--no-follow"])

    assert result.exit_code == 0, result.stderr
    assert result.stdout.splitlines() == [f"line {i:07d}" for i in range(96, 101)]
    assert f"showing {run_dir / 'kas.log'}" in result.stderr
    assert followed == []


def test_no_follow_more_lines_than_first_window(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = _make_run_dir(tmp_path, "r1", {})
    _numbered_log(run_dir, 5000, pad=40)  # ~58 bytes/line, so 2000 lines > 64 KiB
    monkeypatch.setattr(detach_records, "run_dir_for_run", lambda _r: run_dir)
    monkeypatch.setattr(log_module, "_find_workspace_from_cwd", lambda: None)

    result = runner.invoke(app, ["log", "--run", "r1", "--lines", "2000", "--no-follow"])

    assert result.exit_code == 0, result.stderr
    out = result.stdout.splitlines()
    assert len(out) == 2000
    assert out[0].startswith("line 0003001") and out[-1].startswith("line 0005000")


def test_no_follow_read_limit_note(runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_dir = _make_run_dir(tmp_path, "r1", {})
    _numbered_log(run_dir, 200, pad=40)
    monkeypatch.setattr(detach_records, "run_dir_for_run", lambda _r: run_dir)
    monkeypatch.setattr(log_module, "_find_workspace_from_cwd", lambda: None)
    real = log_module._last_lines
    monkeypatch.setattr(log_module, "_last_lines", lambda fh, n: real(fh, n, max_bytes=1000))

    result = runner.invoke(app, ["log", "--run", "r1", "--lines", "500", "--no-follow"])

    assert result.exit_code == 0, result.stderr
    out = result.stdout.splitlines()
    assert 0 < len(out) < 200
    assert all(line.startswith("line ") and line.endswith("x" * 40) for line in out)
    assert f"bakar log: read limit reached, showing the last {len(out)} lines" in result.stderr


def test_lines_below_one_exits_2_without_reading(
    runner: CliRunner, nxp_workspace_with_run: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*_a, **_k):
        raise AssertionError("log read despite invalid --lines")

    monkeypatch.setattr(log_module, "_last_lines", _boom)
    monkeypatch.setattr(log_module, "_tail_follow", _boom)
    calls = _record_stream(monkeypatch)

    for argv in (
        ["log", "--lines", "0", "--workspace", str(nxp_workspace_with_run)],
        ["log", "--lines", "-3", "--on", "pc3"],
    ):
        result = runner.invoke(app, argv)
        assert result.exit_code == 2
        assert "--lines" in result.stderr
    assert calls == []


def test_lines_passed_to_follower(
    runner: CliRunner, nxp_workspace_with_run: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[int] = []

    def _fake(path: Path, history_lines: int = 40) -> None:
        seen.append(history_lines)

    monkeypatch.setattr(log_module, "_tail_follow", _fake)

    result = runner.invoke(app, ["log", "--lines", "100", "--workspace", str(nxp_workspace_with_run)])

    assert result.exit_code == 0, result.stderr
    assert seen == [100]


def test_on_host_forwards_lines_and_no_follow(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _record_stream(monkeypatch)

    result = runner.invoke(app, ["log", "--on", "pc3", "--run", "R", "--lines", "50", "--no-follow"])

    assert result.exit_code == 0, result.stderr
    assert calls == [("pc3", "log", ["--which", "kas", "--run", "R", "--lines", "50", "--no-follow"], 2)]

    calls.clear()
    runner.invoke(app, ["log", "--on", "pc3", "--run", "R", "--lines", "40", "--follow"])
    assert calls == [("pc3", "log", ["--which", "kas", "--run", "R"], 2)]


def test_registered_run_no_follow_outside_workspace(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = _make_run_dir(tmp_path, "r1", {"console.log": "a\nb\nc\n"})  # no kas.log: fallback
    followed = _record_tail(monkeypatch)
    monkeypatch.setattr(detach_records, "newest_run_dir", lambda: run_dir)
    monkeypatch.setattr(log_module, "_find_workspace_from_cwd", lambda: None)

    result = runner.invoke(app, ["log", "--lines", "2", "--no-follow"])

    assert result.exit_code == 0, result.stderr
    assert result.stdout == "b\nc\n"
    assert "console.log" in result.stderr
    assert followed == []


def test_no_follow_broken_pipe_silences_stdout_and_returns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A reader that closed the pipe (``bakar log | head``) must not traceback."""
    run_dir = _make_run_dir(tmp_path, "r1", {})
    _numbered_log(run_dir, 10)
    silenced: list[bool] = []
    # The real helper dup2()s fd 1 onto /dev/null, which would break pytest's capture.
    monkeypatch.setattr(log_module, "_silence_stdout", lambda: silenced.append(True))

    class _ClosedPipe:
        def write(self, _text: str) -> int:
            raise BrokenPipeError

        def flush(self) -> None:
            raise BrokenPipeError

    monkeypatch.setattr(log_module.sys, "stdout", _ClosedPipe())

    log_module._follow_run_dir(run_dir, "kas", 5, False)

    assert silenced == [True]
