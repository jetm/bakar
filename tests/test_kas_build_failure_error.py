"""The kas_build step_fail carries the captured error line (task 3.1).

``_build_fail_error`` decides when the line is attached; ``run_build`` and
``run_shell_live`` pass it as ``error=`` and leave the key out when absent.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

import pytest

from bakar.config import BuildConfig
from bakar.observability import RunLogger
from bakar.steps import kas_build
from bakar.steps.kas_build import KasBuildContext, _PtyOutcome
from bakar.steps.kas_pty import _build_fail_error

if TYPE_CHECKING:
    from pathlib import Path

_ERROR_LINE = "ERROR: Nothing PROVIDES 'does-not-exist'"


def _make_cfg(workspace: Path) -> BuildConfig:
    return BuildConfig(
        workspace=workspace,
        host_mode=False,
        bsp_family="nxp",
        machine="imx95-var-dart",
        distro="fsl-imx-xwayland",
        image="core-image-minimal",
        manifest="imx-6.12.49-2.2.0.xml",
        repo_url="https://example.invalid/none.git",
        repo_branch="walnascar",
        kas_container_image="jetm/kas-build-env:latest",
    )


def _ctx(tmp_path: Path, log: RunLogger, cfg: BuildConfig | None = None) -> KasBuildContext:
    cfg = cfg or _make_cfg(tmp_path)
    cfg.bsp_root.mkdir(parents=True, exist_ok=True)
    kas_yaml = cfg.bsp_root / "build.yml"
    kas_yaml.write_text("header:\n  version: 14\nmachine: qemux86-64\n", encoding="utf-8")
    overlay = cfg.bsp_root / "overlay.yml"
    overlay.write_text("header:\n  version: 14\n", encoding="utf-8")
    return KasBuildContext(cfg=cfg, log=log, kas_yaml=kas_yaml, overlay_source=overlay)


def _step_fails(events_path: Path, step: str = "kas_build") -> list[dict]:
    records = [json.loads(ln) for ln in events_path.read_text().splitlines() if ln]
    return [e for e in records if e.get("event") == "step_fail" and e.get("step") == step]


@pytest.mark.unit
def test_build_fail_error_matrix() -> None:
    assert _build_fail_error(2, None, _ERROR_LINE) == _ERROR_LINE
    assert _build_fail_error(2, [], _ERROR_LINE) == _ERROR_LINE
    assert _build_fail_error(2, None, None) is None
    assert _build_fail_error(2, None, "") is None
    assert _build_fail_error(-2, ["u-boot:do_compile"], _ERROR_LINE) is None
    assert _build_fail_error(None, None, _ERROR_LINE) is None
    # Death by signal (an OOM kill is -9) keeps exit_code=-9 as the reason.
    assert _build_fail_error(-9, None, _ERROR_LINE) is None


def _run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, line: str | None, outcome: _PtyOutcome) -> list[dict]:
    def _fake_pty(pty_ctx: object) -> _PtyOutcome:
        if line is not None:
            pty_ctx.ui.process_line(line)  # type: ignore[attr-defined]
        return outcome

    monkeypatch.setattr(kas_build, "_run_pty_with_ui", _fake_pty)
    with RunLogger(runs_dir=tmp_path / "runs") as log:
        kas_build.run_build(_ctx(tmp_path, log))
        events_path = log.events_path
    return _step_fails(events_path)


@pytest.mark.unit
def test_run_build_records_error_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fails = _run(tmp_path, monkeypatch, line=_ERROR_LINE, outcome=_PtyOutcome(rc=2))
    assert len(fails) == 1
    assert fails[0]["reason"] == "exit_code=2"
    assert fails[0]["error"] == _ERROR_LINE


@pytest.mark.unit
def test_run_build_without_error_line_omits_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fails = _run(tmp_path, monkeypatch, line=None, outcome=_PtyOutcome(rc=2))
    assert len(fails) == 1
    assert fails[0]["reason"] == "exit_code=2"
    assert "error" not in fails[0]


@pytest.mark.unit
def test_run_build_stall_keeps_stall_reason_and_no_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fails = _run(
        tmp_path,
        monkeypatch,
        line=_ERROR_LINE,
        outcome=_PtyOutcome(rc=-2, stall_tasks=["u-boot:do_compile"]),
    )
    assert len(fails) == 1
    assert fails[0]["reason"] == "stall-timeout: u-boot:do_compile"
    assert "error" not in fails[0]


@pytest.mark.unit
def test_run_build_zero_exit_writes_no_step_fail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _run(tmp_path, monkeypatch, line=_ERROR_LINE, outcome=_PtyOutcome(rc=0)) == []


@pytest.mark.unit
def test_run_build_through_real_pty_pump_records_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Drive the real _run_pty_with_ui so the streaming seam itself is covered."""
    monkeypatch.setattr(kas_build.build_stop, "write_pid", lambda run_dir, pgid: None)
    monkeypatch.setattr(kas_build.build_stop, "remove_pid", lambda run_dir: None)
    monkeypatch.setattr(kas_build.build_stop, "check_unclean_stop", lambda *a, **kw: None)
    monkeypatch.setattr(
        kas_build, "clear_stale_bitbake_locks", lambda cfg: kas_build.build_stop.LockClearOutcome(removed=[])
    )
    monkeypatch.setattr(kas_build, "cache_mount_refusal", lambda cfg, **_kw: None)

    class _FakeProc:
        pid = 424242

        def wait(self, timeout: float | None = None) -> int:
            return 2

        def poll(self) -> int:
            return 2

    def _fake_popen(*_args: object, **kw: object) -> _FakeProc:
        os.write(kw["stdout"], b"ERROR: boom\r\n")  # type: ignore[arg-type]
        return _FakeProc()

    monkeypatch.setattr(kas_build.subprocess, "Popen", _fake_popen)

    with RunLogger(runs_dir=tmp_path / "runs") as log:
        rc = kas_build.run_build(_ctx(tmp_path, log))
        events_path = log.events_path

    assert rc == 2
    fails = _step_fails(events_path)
    assert len(fails) == 1
    assert fails[0]["reason"] == "exit_code=2"
    assert fails[0]["error"] == "ERROR: boom"


@pytest.mark.unit
def test_run_shell_live_through_real_pty_pump_records_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The live-shell fallback step_fail carries the captured line the same way."""
    monkeypatch.setattr(kas_build.build_stop, "write_pid", lambda run_dir, pgid: None)
    monkeypatch.setattr(kas_build.build_stop, "remove_pid", lambda run_dir: None)
    monkeypatch.setattr(kas_build.build_stop, "check_unclean_stop", lambda *a, **kw: None)
    monkeypatch.setattr(
        kas_build, "clear_stale_bitbake_locks", lambda cfg: kas_build.build_stop.LockClearOutcome(removed=[])
    )
    monkeypatch.setattr(kas_build, "cache_mount_refusal", lambda cfg, **_kw: None)

    class _FakeProc:
        pid = 424243

        def wait(self, timeout: float | None = None) -> int:
            return 3

        def poll(self) -> int:
            return 3

    def _fake_popen(*_args: object, **kw: object) -> _FakeProc:
        os.write(kw["stdout"], b"ERROR: shell boom\r\n")  # type: ignore[arg-type]
        return _FakeProc()

    monkeypatch.setattr(kas_build.subprocess, "Popen", _fake_popen)

    with RunLogger(runs_dir=tmp_path / "runs") as log:
        rc = kas_build.run_shell_live(_ctx(tmp_path, log), "bitbake -e")
        events_path = log.events_path

    assert rc == 3
    fails = _step_fails(events_path, step="kas_shell_live")
    assert len(fails) == 1
    assert fails[0]["reason"] == "exit_code=3"
    assert fails[0]["error"] == "ERROR: shell boom"
