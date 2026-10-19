"""Tests for ``_record_build_paths``: the build_paths run event written before kas starts."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from bakar.observability import RunLogger, iter_run_events, last_build_paths
from bakar.steps import kas_build
from bakar.steps.kas_build import _record_build_paths

if TYPE_CHECKING:
    from pathlib import Path


def _events(run_dir: Path, name: str) -> list[dict[str, object]]:
    return [r for r in iter_run_events(run_dir / "events.jsonl") if r.get("event") == name]


@pytest.mark.unit
def test_exported_sstate_dir_records_both_paths_absolute(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sstate = tmp_path / "env-sstate"
    monkeypatch.setenv("SSTATE_DIR", str(sstate))
    cfg = SimpleNamespace(resolved_tmpdir=tmp_path / "build" / "tmp", sstate_dir=None)
    with RunLogger(tmp_path / "runs") as log:
        _record_build_paths(cfg, log)  # type: ignore[arg-type]
        run_dir = log.run_dir

    recs = _events(run_dir, "build_paths")
    assert len(recs) == 1
    assert recs[0]["tmpdir"] == str(tmp_path / "build" / "tmp")
    assert recs[0]["sstate_dir"] == str(sstate)
    assert last_build_paths(run_dir) == (tmp_path / "build" / "tmp", sstate)


@pytest.mark.unit
def test_neither_env_nor_config_records_sstate_null(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SSTATE_DIR", raising=False)
    cfg = SimpleNamespace(resolved_tmpdir=tmp_path / "tmp", sstate_dir=None)
    with RunLogger(tmp_path / "runs") as log:
        _record_build_paths(cfg, log)  # type: ignore[arg-type]
        run_dir = log.run_dir

    recs = _events(run_dir, "build_paths")
    assert len(recs) == 1
    assert recs[0]["sstate_dir"] is None
    assert recs[0]["tmpdir"] == str(tmp_path / "tmp")


@pytest.mark.unit
def test_raising_resolved_tmpdir_records_warn_and_raises_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SSTATE_DIR", raising=False)

    class _Cfg:
        sstate_dir = None

        @property
        def resolved_tmpdir(self) -> Path:
            raise RuntimeError("tmpdir boom")

    with RunLogger(tmp_path / "runs") as log:
        _record_build_paths(_Cfg(), log)  # type: ignore[arg-type]
        run_dir = log.run_dir

    assert _events(run_dir, "build_paths") == []
    warns = _events(run_dir, "warn")
    assert len(warns) == 1
    assert "tmpdir boom" in str(warns[0]["message"])


class _StopHereError(Exception):
    """Raised by the recorder spy so the entry point stops before it could spawn kas."""


def _spy_entry(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    order: list[str] = []

    def record(_cfg: object, _log: object) -> None:
        order.append("record")
        raise _StopHereError

    def spawn(*_a: object, **_k: object) -> None:
        order.append("spawn")
        raise _StopHereError

    monkeypatch.setattr(kas_build, "_record_build_paths", record)
    monkeypatch.setattr(kas_build, "cache_mount_refusal", lambda _cfg: None)
    monkeypatch.setattr(kas_build, "clear_stale_bitbake_locks", lambda _cfg: SimpleNamespace(removed=[], refusal=None))
    monkeypatch.setattr(kas_build.build_stop, "check_unclean_stop", lambda *_a, **_k: None)
    monkeypatch.setattr(kas_build, "_run_pty_with_ui", spawn)
    monkeypatch.setattr(kas_build.subprocess, "Popen", spawn)
    return order


def _ctx(tmp_path: Path) -> SimpleNamespace:
    cfg = SimpleNamespace(sstate_mirrors_source="none", bsp_root=tmp_path, host_mode=True)
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
def test_run_build_records_paths_before_any_spawn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    order = _spy_entry(monkeypatch)
    with pytest.raises(_StopHereError):
        kas_build.run_build(_ctx(tmp_path))  # type: ignore[arg-type]
    assert order == ["record"]


@pytest.mark.unit
def test_run_shell_live_records_paths_before_any_spawn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    order = _spy_entry(monkeypatch)
    with pytest.raises(_StopHereError):
        kas_build.run_shell_live(_ctx(tmp_path), "bitbake -e")  # type: ignore[arg-type]
    assert order == ["record"]
