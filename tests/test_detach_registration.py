"""Run registration with a detached unit: RunLogger.__enter__ and the root callback."""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from bakar import detach_records
from bakar.cli import app
from bakar.observability import RunLogger

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit

UNIT = "bakar-detach-20261019-120000-abc123"


@pytest.fixture(autouse=True)
def _runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rt = tmp_path / "rt"
    rt.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(rt))
    monkeypatch.setitem(detach_records._state, "unit", None)
    return rt


def _lines(rt: Path) -> list[dict]:
    reg = rt / f"{UNIT}.runs"
    return [json.loads(x) for x in reg.read_text().splitlines()]


def test_logger_appends_registration(_runtime: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(detach_records.REGISTRATION_ENV, UNIT)
    detach_records.consume_registration_env()
    with RunLogger(tmp_path / "runs") as log:
        pass
    recs = _lines(_runtime)
    assert len(recs) == 1
    assert recs[0]["run_id"] == log.run_id
    assert recs[0]["pid"] == os.getpid()
    assert log.run_id.endswith(f"-{os.getpid()}")
    assert recs[0]["run_dir"] == str(log.run_dir)


def test_two_loggers_append_two(_runtime: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(detach_records.REGISTRATION_ENV, UNIT)
    detach_records.consume_registration_env()
    with RunLogger(tmp_path / "a"), RunLogger(tmp_path / "b"):
        pass
    assert len(_lines(_runtime)) == 2


def test_no_unit_writes_no_registry(_runtime: Path, tmp_path: Path) -> None:
    with RunLogger(tmp_path / "runs"):
        pass
    assert list(_runtime.glob("*.runs")) == []


def test_registration_failure_does_not_raise(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a, **_k):
        raise OSError("disk gone")

    monkeypatch.setattr(detach_records, "register_current_run", boom)
    with RunLogger(tmp_path / "runs") as log:
        pass
    assert log.events_path.exists()


def test_false_return_with_unit_warns(_runtime: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(detach_records._state, "unit", UNIT)
    monkeypatch.setattr(detach_records, "register_current_run", lambda *a, **k: False)
    with RunLogger(tmp_path / "runs") as log:
        pass
    assert "detach registration could not be written" in log.console_path.read_text()


def test_root_callback_consumes_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(detach_records.REGISTRATION_ENV, UNIT)
    # Conflicting flags make _main_impl exit 2 right after the consume step.
    CliRunner().invoke(app, ["--plain", "--rich", "doctor"])
    assert detach_records.REGISTRATION_ENV not in os.environ
    assert detach_records.current_unit() == UNIT
