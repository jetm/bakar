"""Tests for ``_record_build_paths``: the build_paths run event written before kas starts."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from bakar.observability import RunLogger, iter_run_events, last_build_paths
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
