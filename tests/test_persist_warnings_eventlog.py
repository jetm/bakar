"""Post-build persistence failures land in the run's events.jsonl as warn records."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from bakar.observability import RunLogger
from bakar.steps import kas_build

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _events(log: RunLogger) -> list[dict[str, Any]]:
    return [json.loads(line) for line in log.events_path.read_text().splitlines() if line.strip()]


def test_native_ledger_failure_is_a_warn_record_without_terminal_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: Any, **kwargs: Any) -> None:
        raise OSError("disk on fire")

    monkeypatch.setattr(kas_build.native_ledger, "capture_run", boom)
    monkeypatch.setattr(kas_build.native_ledger, "stamp_roots", lambda *a, **k: [])
    cfg = SimpleNamespace(resolved_tmpdir=tmp_path, bsp_root=tmp_path, build_dir_name="build")
    with RunLogger(runs_dir=tmp_path / "runs") as log:
        kas_build._capture_native_ledger(cfg, log, {"tasks": []}, tmp_path / "sstate")  # type: ignore[arg-type]
        events = _events(log)

    warns = [e for e in events if e["event"] == "warn" and "native signature ledger" in e["message"]]
    assert len(warns) == 1
    assert "disk on fire" in warns[0]["message"]
    assert not [e for e in events if e["event"] in ("step_ok", "step_fail") and e.get("step") == "kas_build"]
