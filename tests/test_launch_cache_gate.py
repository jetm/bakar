"""Tests for the cache-mount/daemon-state launch gate (task 3.2).

``cache_mount_refusal(cfg)`` (``bakar.steps.kas_build``) is called at the
head of every bitbake launch function - ``run_build``, ``run_shell_live``,
``run_shell_capture`` - and by ``bakar getvar``'s ``_getvar_impl``, before
``clear_stale_bitbake_locks`` and before any environment assembly or PTY
launch. It gates on cache-mount readiness first (cache-mount-readiness spec,
launch-gate requirement), then falls through to
``bakar.hashserv.daemon_state_refusal`` (daemon-state-filesystem-guard spec,
hard-stop requirement) only when no cache mount is blocking.

Both underlying probes are patched at their owning module (``bakar.mounts``,
``bakar.hashserv``) rather than as a name imported into ``kas_build`` -
``cache_mount_refusal`` calls them through the module attribute
(``mounts.assess_cache_mounts``, ``hashserv.daemon_state_refusal``), matching
the pattern the rest of this file's dependencies already use (see
``bakar.hashserv.network_state_reason``'s own docstring on this point).
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
import typer

from bakar import build_stop
from bakar.commands.getvar import _getvar_impl, _GetvarCtx
from bakar.config import BuildConfig
from bakar.mounts import CacheMountStatus
from bakar.observability import RunLogger
from bakar.steps import kas_build
from bakar.steps.kas_build import KasBuildContext

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


def _make_cfg(workspace: Path) -> BuildConfig:
    """Minimal NXP BuildConfig anchored at ``workspace``."""
    bsp_root = workspace / "nxp"
    bsp_root.mkdir(parents=True, exist_ok=True)
    return BuildConfig(
        workspace=workspace,
        bsp_family="nxp",  # type: ignore[arg-type]
        machine="imx8mp-var-dart",
        distro="fsl-imx-xwayland",
        image="core-image-minimal",
        manifest="imx-6.6.52-2.2.2.xml",
        repo_url="https://example.invalid/repo.git",
        repo_branch="scarthgap",
        kas_container_image="jetm/kas-build-env:latest",
    )


def _ctx(cfg: BuildConfig, log: RunLogger, *, dry_run: bool = False) -> KasBuildContext:
    kas_yaml = cfg.bsp_root / "build.yml"
    kas_yaml.write_text("header:\n  version: 14\nmachine: imx8mp-var-dart\n", encoding="utf-8")
    overlay = cfg.bsp_root / "overlay.yml"
    overlay.write_text("header:\n  version: 14\n", encoding="utf-8")
    return KasBuildContext(cfg=cfg, log=log, kas_yaml=kas_yaml, overlay_source=overlay, dry_run=dry_run)


def _unresponsive_status(path: Path) -> CacheMountStatus:
    return CacheMountStatus(
        label="sstate_dir",
        path=path,
        state="unresponsive",
        fstype=None,
        mountpoint=None,
        source="nas:/export",
        declared_nfs=True,
        fstab_source="nas:/export",
        detail="",
    )


def _never_called(name: str):
    def _fail(*_args: object, **_kwargs: object) -> object:
        raise AssertionError(f"{name} must not be called after a cache-mount refusal")

    return _fail


# ---------------------------------------------------------------------------
# Unresponsive cache mount blocks every launch function
# ---------------------------------------------------------------------------


def test_run_build_refuses_on_unresponsive_cache_mount(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _make_cfg(tmp_path)
    status = _unresponsive_status(cfg.bsp_root / "sstate")
    monkeypatch.setattr("bakar.mounts.assess_cache_mounts", lambda _targets, **_kw: [status])
    monkeypatch.setattr("bakar.steps.kas_build.clear_stale_bitbake_locks", _never_called("clear_stale_bitbake_locks"))
    monkeypatch.setattr("bakar.steps.kas_build._run_pty_with_ui", _never_called("_run_pty_with_ui"))

    with RunLogger(runs_dir=cfg.runs_dir) as log:
        ctx = _ctx(cfg, log)
        rc = kas_build.run_build(ctx)
        events_path = log.events_path

    assert rc == 1
    events = [e for e in _read_events(events_path) if e.get("step") == "kas_build" and e.get("event") == "step_fail"]
    assert len(events) == 1
    assert str(status.path) in events[0]["reason"]


def test_run_shell_live_refuses_on_unresponsive_cache_mount(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _make_cfg(tmp_path)
    status = _unresponsive_status(cfg.bsp_root / "sstate")
    monkeypatch.setattr("bakar.mounts.assess_cache_mounts", lambda _targets, **_kw: [status])
    monkeypatch.setattr("bakar.steps.kas_build.clear_stale_bitbake_locks", _never_called("clear_stale_bitbake_locks"))
    monkeypatch.setattr("bakar.steps.kas_build._run_pty_with_ui", _never_called("_run_pty_with_ui"))

    with RunLogger(runs_dir=cfg.runs_dir) as log:
        ctx = _ctx(cfg, log)
        rc = kas_build.run_shell_live(ctx, "bitbake core-image-minimal")
        events_path = log.events_path

    assert rc == 1
    events = [
        e for e in _read_events(events_path) if e.get("step") == "kas_shell_live" and e.get("event") == "step_fail"
    ]
    assert len(events) == 1
    assert str(status.path) in events[0]["reason"]


def test_run_shell_capture_refuses_on_unresponsive_cache_mount(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _make_cfg(tmp_path)
    status = _unresponsive_status(cfg.bsp_root / "sstate")
    monkeypatch.setattr("bakar.mounts.assess_cache_mounts", lambda _targets, **_kw: [status])
    monkeypatch.setattr("bakar.steps.kas_build.clear_stale_bitbake_locks", _never_called("clear_stale_bitbake_locks"))
    monkeypatch.setattr("bakar.steps.kas_build.subprocess.Popen", _never_called("subprocess.Popen"))

    with RunLogger(runs_dir=cfg.runs_dir) as log:
        ctx = _ctx(cfg, log)
        stdout_path = tmp_path / "capture.log"
        rc = kas_build.run_shell_capture(ctx, "bitbake -c listtasks foo", stdout_path)
        events_path = log.events_path

    assert rc == 1
    events = [
        e for e in _read_events(events_path) if e.get("step") == "kas_shell_capture" and e.get("event") == "step_fail"
    ]
    assert len(events) == 1
    assert str(status.path) in events[0]["reason"]
    assert events[0].get("exit_code") == 1


# ---------------------------------------------------------------------------
# Healthy status: gate runs before clear_stale_bitbake_locks
# ---------------------------------------------------------------------------


def test_gate_runs_before_clear_stale_bitbake_locks_on_healthy_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _make_cfg(tmp_path)
    call_order: list[str] = []

    def _fake_assess(_targets: object, **_kw: object) -> list[CacheMountStatus]:
        call_order.append("cache_mount_refusal")
        return []

    def _fake_daemon_state_refusal(_cfg: object) -> None:
        call_order.append("daemon_state_refusal")
        return None

    def _fake_clear_stale_locks(_cfg: object) -> build_stop.LockClearOutcome:
        call_order.append("clear_stale_bitbake_locks")
        # Abort right after, to avoid driving the real PTY/threading launch
        # path - only the ordering before this point is under test.
        return build_stop.LockClearOutcome(removed=[], refusal=build_stop.LockRefusal(reason="peer-held", host="x"))

    monkeypatch.setattr("bakar.mounts.assess_cache_mounts", _fake_assess)
    monkeypatch.setattr("bakar.hashserv.daemon_state_refusal", _fake_daemon_state_refusal)
    monkeypatch.setattr("bakar.steps.kas_build.clear_stale_bitbake_locks", _fake_clear_stale_locks)

    with RunLogger(runs_dir=cfg.runs_dir) as log:
        ctx = _ctx(cfg, log)
        rc = kas_build.run_build(ctx)

    assert rc == 1
    assert call_order == ["cache_mount_refusal", "daemon_state_refusal", "clear_stale_bitbake_locks"]


# ---------------------------------------------------------------------------
# Dry-run never calls the gate
# ---------------------------------------------------------------------------


def test_run_build_dry_run_never_calls_the_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _make_cfg(tmp_path)
    monkeypatch.setattr(
        "bakar.mounts.assess_cache_mounts",
        _never_called("assess_cache_mounts"),
    )
    monkeypatch.setattr(
        "bakar.hashserv.daemon_state_refusal",
        _never_called("daemon_state_refusal"),
    )
    monkeypatch.setattr("bakar.steps.kas_build.subprocess.Popen", _never_called("subprocess.Popen"))

    with RunLogger(runs_dir=cfg.runs_dir) as log:
        ctx = _ctx(cfg, log, dry_run=True)
        rc = kas_build.run_build(ctx)

    assert rc == 0


# ---------------------------------------------------------------------------
# getvar: a refusal leaves no new run directory
# ---------------------------------------------------------------------------


def test_getvar_refusal_creates_no_run_directory(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
    status = _unresponsive_status(cfg.bsp_root / "sstate")

    with (
        patch("bakar.mounts.assess_cache_mounts", lambda _targets, **_kw: [status]),
        patch("bakar.steps.kas_build.clear_stale_bitbake_locks", _never_called("clear_stale_bitbake_locks")),
        patch("bakar.commands.getvar.run_shell_capture", _never_called("run_shell_capture")),
        pytest.raises(typer.Exit) as exc_info,
    ):
        _getvar_impl(
            _GetvarCtx(
                var="MACHINE",
                kas_yaml=None,
                recipe=None,
                unexpanded=False,
                flag=None,
                history=False,
                manifest="imx-6.6.52-2.2.2.xml",
                machine=None,
                workspace=tmp_path,
                output_json=False,
            )
        )

    assert exc_info.value.exit_code == 1
    assert not cfg.runs_dir.exists()


def _read_events(events_path: Path) -> list[dict]:
    import json

    return [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines() if line]
