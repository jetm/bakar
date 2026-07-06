"""Tests for the plain-mode frame controller in kas_build, plus unit tests
locking in the terminated-ordering fix (task 2.1) and the run_shell /
run_shell_capture step_fail-on-nonzero-rc fix (tasks 2.3/2.4).
"""

from __future__ import annotations

import json
import threading
from io import StringIO
from typing import TYPE_CHECKING

import pytest
from rich.console import Console
from rich.table import Table

from bakar.config import BuildConfig
from bakar.observability import RunLogger
from bakar.steps import kas_build
from bakar.steps.kas_build import BuildUIState, KasBuildContext, _PlainFrameController

if TYPE_CHECKING:
    from pathlib import Path

_ESC = "\x1b"


def _controller(buf: StringIO) -> _PlainFrameController:
    console = Console(no_color=True, force_terminal=False, file=buf)
    stop = threading.Event()
    stop.set()  # loop exits on the first wait, so the thread joins immediately
    return _PlainFrameController(BuildUIState(start_monotonic=1.0), console, stop)


def test_shim_start_refresh_and_transient_do_not_raise() -> None:
    # R1: the restart path calls live.start(refresh=True) and writes live.transient.
    with _controller(StringIO()) as live:
        live.stop()
        live.start(refresh=True)
        live.transient = True


def test_shim_renders_table_plainly() -> None:
    # The closures pass Rich Table renderables (layer_hash_table); a no-color
    # console must render them to plain text, not repr, and emit no ANSI.
    buf = StringIO()
    with _controller(buf) as live:
        table = Table()
        table.add_column("layer")
        table.add_row("layerhash123")
        live.console.print(table)
    out = buf.getvalue()
    assert _ESC not in out
    assert "layerhash123" in out
    assert "rich.table.Table" not in out


def test_shim_console_prints_plain_status_without_ansi() -> None:
    # The status line has literal brackets ("bakar[build]"); the loop prints with
    # markup=False so Rich does not eat them as a style tag. Mirror that here.
    buf = StringIO()
    with _controller(buf) as live:
        live.console.print("bakar[build] phase=tasks tasks=1/2 running=1 elapsed=0s", markup=False)
    out = buf.getvalue()
    assert _ESC not in out
    assert "bakar[build]" in out
    assert "phase=tasks" in out


# ---------------------------------------------------------------------------
# Shared helpers for the run_shell / run_shell_capture / run_build tests below
# ---------------------------------------------------------------------------


def _make_cfg(workspace: Path) -> BuildConfig:
    """Construct a minimal NXP BuildConfig rooted at ``workspace``."""
    return BuildConfig(
        workspace=workspace,
        bsp_family="nxp",
        machine="imx95-var-dart",
        distro="fsl-imx-xwayland",
        image="core-image-minimal",
        manifest="imx-6.12.49-2.2.0.xml",
        repo_url="https://example.invalid/none.git",
        repo_branch="walnascar",
        kas_container_image="jetm/kas-build-env:latest",
    )


def _prepare_workspace(tmp_path: Path) -> tuple[BuildConfig, Path, Path]:
    """Build a cfg plus a kas YAML inside bsp_root and an overlay source."""
    cfg = _make_cfg(tmp_path)
    cfg.bsp_root.mkdir(parents=True, exist_ok=True)
    kas_yaml = cfg.bsp_root / "build.yml"
    kas_yaml.write_text("header: {}\n", encoding="utf-8")
    overlay = tmp_path / "bakar-tuning-nxp.yml"
    overlay.write_text("header: {}\n", encoding="utf-8")
    return cfg, kas_yaml, overlay


def _make_ctx(tmp_path: Path, log: RunLogger) -> KasBuildContext:
    cfg, kas_yaml, overlay = _prepare_workspace(tmp_path)
    return KasBuildContext(cfg=cfg, log=log, kas_yaml=kas_yaml, overlay_source=overlay)


class _FakeProc:
    """Stand-in for ``subprocess.Popen`` that returns a fixed exit code."""

    def __init__(self, rc: int) -> None:
        self._rc = rc

    def wait(self) -> int:
        return self._rc


def _step_events(events_path: Path) -> list[dict]:
    """Parse events.jsonl, keeping only step_* records (matches test_observability.py)."""
    parsed = [json.loads(ln) for ln in events_path.read_text().splitlines() if ln]
    return [e for e in parsed if e.get("event") in {"step_start", "step_ok", "step_fail", "step_skip"}]


# ---------------------------------------------------------------------------
# task 2.3 / 2.4: run_shell and run_shell_capture emit step_fail on nonzero rc
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("rc", [0, 1, 137])
def test_run_shell_emits_step_ok_or_step_fail_matching_rc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rc: int
) -> None:
    monkeypatch.setattr(kas_build.subprocess, "Popen", lambda *args, **kwargs: _FakeProc(rc))

    with RunLogger(runs_dir=tmp_path / "runs") as log:
        ctx = _make_ctx(tmp_path, log)
        result = kas_build.run_shell(ctx, args=[])
        events_path = log.events_path

    assert result == rc
    events = _step_events(events_path)
    terminal = [e for e in events if e["step"] == "kas_shell" and e["event"] != "step_start"]
    assert len(terminal) == 1, f"expected exactly one terminal kas_shell event, got {terminal!r}"
    if rc == 0:
        assert terminal[0]["event"] == "step_ok"
    else:
        assert terminal[0]["event"] == "step_fail"
        assert terminal[0].get("reason") == f"exit_code={rc}"
        assert terminal[0].get("exit_code") == rc


@pytest.mark.unit
@pytest.mark.parametrize("rc", [0, 1, 137])
def test_run_shell_capture_emits_step_ok_or_step_fail_matching_rc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rc: int
) -> None:
    monkeypatch.setattr(kas_build.subprocess, "Popen", lambda *args, **kwargs: _FakeProc(rc))

    with RunLogger(runs_dir=tmp_path / "runs") as log:
        ctx = _make_ctx(tmp_path, log)
        stdout_path = tmp_path / "capture.log"
        result = kas_build.run_shell_capture(ctx, command="bitbake -c listtasks foo", stdout_path=stdout_path)
        events_path = log.events_path

    assert result == rc
    events = _step_events(events_path)
    terminal = [e for e in events if e["step"] == "kas_shell_capture" and e["event"] != "step_start"]
    assert len(terminal) == 1, f"expected exactly one terminal kas_shell_capture event, got {terminal!r}"
    if rc == 0:
        assert terminal[0]["event"] == "step_ok"
    else:
        assert terminal[0]["event"] == "step_fail"
        assert terminal[0].get("reason") == f"exit_code={rc}"
        assert terminal[0].get("exit_code") == rc


# ---------------------------------------------------------------------------
# task 2.1: terminated=True set before the persistence tail - no duplicate
# terminal kas_build event when the persistence tail raises
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_run_build_persist_tail_failure_does_not_duplicate_terminal_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, kas_yaml, overlay = _prepare_workspace(tmp_path)

    def _fake_run_pty(*args: object, **kwargs: object) -> kas_build._PtyOutcome:
        return kas_build._PtyOutcome(rc=0)

    def _boom_persist(*_args: object, **_kwargs: object) -> object:
        raise OSError("disk full")

    monkeypatch.setattr(kas_build, "_run_pty_with_ui", _fake_run_pty)
    monkeypatch.setattr(kas_build, "copy_oe_eventlog_to_run_dir", _boom_persist)

    with RunLogger(runs_dir=cfg.runs_dir) as log:
        ctx = KasBuildContext(cfg=cfg, log=log, kas_yaml=kas_yaml, overlay_source=overlay)
        rc = kas_build.run_build(ctx)
        events_path = log.events_path

    assert rc == 0
    events = _step_events(events_path)
    terminal = [e for e in events if e["step"] == "kas_build" and e["event"] != "step_start"]
    assert len(terminal) == 1, f"expected exactly one terminal kas_build event, got {terminal!r}"
    assert terminal[0]["event"] == "step_ok"
