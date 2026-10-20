"""Tests for ``_record_build_config``: the build_config run event written before kas starts."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from bakar.observability import RunLogger, iter_run_events
from bakar.steps import kas_build
from bakar.steps.kas_build import _record_build_config

if TYPE_CHECKING:
    from pathlib import Path


def _events(run_dir: Path, name: str) -> list[dict[str, object]]:
    return [r for r in iter_run_events(run_dir / "events.jsonl") if r.get("event") == name]


def _cfg(tmp_path: Path, **over: object) -> SimpleNamespace:
    base: dict[str, object] = {
        "bsp_family": "nxp",
        "machine": "imx93-var-som",
        "host_mode": True,
        "kas_work_dir": tmp_path / "work",
    }
    base.update(over)
    return SimpleNamespace(**base)


@pytest.mark.unit
def test_host_mode_records_machine_mode_and_null_mount(tmp_path: Path) -> None:
    with RunLogger(tmp_path / "runs") as log:
        _record_build_config(_cfg(tmp_path), log)  # type: ignore[arg-type]
        run_dir = log.run_dir

    recs = _events(run_dir, "build_config")
    assert len(recs) == 1
    assert recs[0]["family"] == "nxp"
    assert recs[0]["machine"] == "imx93-var-som"
    assert recs[0]["mode"] == "host"
    assert recs[0]["work_mount"] is None


@pytest.mark.unit
def test_container_mode_records_kas_work_dir(tmp_path: Path) -> None:
    with RunLogger(tmp_path / "runs") as log:
        _record_build_config(_cfg(tmp_path, host_mode=False, bsp_family="ti"), log)  # type: ignore[arg-type]
        run_dir = log.run_dir

    recs = _events(run_dir, "build_config")
    assert recs[0]["family"] == "ti"
    assert recs[0]["mode"] == "container"
    assert recs[0]["work_mount"] == str(tmp_path / "work")


@pytest.mark.unit
def test_generic_machine_is_recorded_as_unknown_and_other_family_generic(tmp_path: Path) -> None:
    with RunLogger(tmp_path / "runs") as log:
        _record_build_config(_cfg(tmp_path, machine="generic", bsp_family="bbsetup"), log)  # type: ignore[arg-type]
        run_dir = log.run_dir

    recs = _events(run_dir, "build_config")
    assert recs[0]["machine"] == "unknown"
    assert recs[0]["family"] == "generic"


@pytest.mark.unit
def test_raising_writer_becomes_one_warn(tmp_path: Path) -> None:
    log = MagicMock()
    log.build_config.side_effect = RuntimeError("writer boom")
    _record_build_config(_cfg(tmp_path), log)  # type: ignore[arg-type]
    log.warn.assert_called_once()
    assert "writer boom" in log.warn.call_args.args[0]


class _StopHereError(Exception):
    """Raised by the spies so the entry point stops before it could spawn kas."""


def _spy_entry(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    order: list[str] = []

    def record(_cfg: object, _log: object) -> None:
        order.append("config")

    def stop(*_a: object, **_k: object) -> None:
        order.append("after")
        raise _StopHereError

    monkeypatch.setattr(kas_build, "_record_build_paths", lambda _c, _l: None)
    monkeypatch.setattr(kas_build, "_record_build_config", record)
    monkeypatch.setattr(kas_build, "cache_mount_refusal", lambda _cfg: None)
    monkeypatch.setattr(kas_build, "clear_stale_bitbake_locks", lambda _cfg: SimpleNamespace(removed=[], refusal=None))
    monkeypatch.setattr(kas_build.build_stop, "check_unclean_stop", lambda *_a, **_k: None)
    monkeypatch.setattr(kas_build, "_build_kas_arg", stop)
    monkeypatch.setattr(kas_build, "_run_pty_with_ui", stop)
    monkeypatch.setattr(kas_build.subprocess, "Popen", stop)
    return order


def _ctx(tmp_path: Path) -> SimpleNamespace:
    cfg = SimpleNamespace(
        sstate_mirrors_source="none", bsp_root=tmp_path, host_mode=True, measurements_dir=tmp_path / "m"
    )
    return SimpleNamespace(
        cfg=cfg,
        log=MagicMock(),
        kas_yaml=tmp_path / "k.yml",
        overlay_source=tmp_path / "o.yml",
        step="kas_build",
        dry_run=False,
        keep_going=False,
        target=None,
        extra_overlays=[],
    )


@pytest.mark.unit
def test_run_build_records_config_once_before_kas_arg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    order = _spy_entry(monkeypatch)
    with pytest.raises(_StopHereError):
        kas_build.run_build(_ctx(tmp_path))  # type: ignore[arg-type]
    assert order == ["config", "after"]


@pytest.mark.unit
def test_run_shell_live_records_config_once_before_kas_arg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    order = _spy_entry(monkeypatch)
    with pytest.raises(_StopHereError):
        kas_build.run_shell_live(_ctx(tmp_path), "bitbake -e")  # type: ignore[arg-type]
    assert order == ["config", "after"]
