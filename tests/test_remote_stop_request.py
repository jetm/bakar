"""The remote stop script records the stop request on the host before signalling.

Runs the generated script with real bash against a fake ``systemctl`` first on
PATH, so the ordering (record, then signal) and the quoting of registry values
are exercised for real rather than by string matching.
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import TYPE_CHECKING

import pytest

from bakar import detach_records, observability
from bakar.steps.remote_dispatch import build_stop_units_script

if TYPE_CHECKING:
    from pathlib import Path

UNIT = "bakar-20260101-000000-abcdef.service"
UNIT_BASE = "bakar-20260101-000000-abcdef"

FAKE_SYSTEMCTL = """#!/usr/bin/env bash
# Logs each call; unit is active until the first kill or stop.
echo "$*" >> "$FAKE_DIR/calls.log"
case "$2" in
  kill|stop)
    if [ ! -e "$FAKE_DIR/signalled" ]; then
      : > "$FAKE_DIR/signalled"
      while IFS= read -r d; do
        if [ -e "$d/stop-request.json" ]; then echo "present" >> "$FAKE_DIR/seen.log"
        else echo "absent" >> "$FAKE_DIR/seen.log"; fi
      done < "$FAKE_DIR/dirs.txt"
    fi
    ;;
  is-active)
    [ ! -e "$FAKE_DIR/signalled" ] && exit 0
    exit 3
    ;;
esac
exit 0
"""


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    fake_dir = tmp_path / "fake"
    fake_dir.mkdir()
    (fake_dir / "dirs.txt").write_text("")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    systemctl = bin_dir / "systemctl"
    systemctl.write_text(FAKE_SYSTEMCTL)
    systemctl.chmod(0o755)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    return {"runtime": runtime, "fake": fake_dir, "bin": bin_dir, "root": tmp_path}


def _run(host: dict[str, Path], *, force: bool = False) -> subprocess.CompletedProcess[str]:
    script = build_stop_units_script([UNIT], force=force, grace_seconds=2)
    env = {
        "PATH": f"{host['bin']}:{os.environ['PATH']}",
        "XDG_RUNTIME_DIR": str(host["runtime"]),
        "FAKE_DIR": str(host["fake"]),
        "HOME": str(host["root"]),
    }
    return subprocess.run(
        ["bash", "-s"],
        input=script,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        cwd=host["root"],
        check=False,
    )


def _calls(host: dict[str, Path]) -> list[str]:
    return (host["fake"] / "calls.log").read_text().splitlines()


def _register(host: dict[str, Path], *dirs: Path) -> None:
    (host["fake"] / "dirs.txt").write_text("".join(f"{d}\n" for d in dirs))
    for i, d in enumerate(dirs):
        assert detach_records.append_registration(UNIT_BASE, f"run{i}", d, 100 + i)


def _mkrun(host: dict[str, Path], name: str) -> Path:
    d = host["root"] / name
    d.mkdir()
    return d


def test_every_registered_run_gets_a_record_before_the_kill(host: dict[str, Path]) -> None:
    a, b = _mkrun(host, "run-a"), _mkrun(host, "run-b")
    _register(host, a, b)
    result = _run(host)
    assert result.returncode == 0, result.stderr
    for d in (a, b):
        rec = observability.read_stop_request(d)
        assert rec is not None
        assert rec["by"] == observability.STOP_REQUEST_BY
        assert rec["force"] is False
        assert rec["requested_at"].endswith("Z")
    assert (host["fake"] / "seen.log").read_text().split() == ["present", "present"]
    assert any("kill" in c for c in _calls(host))


def test_runs_that_already_ended_are_not_marked(host: dict[str, Path]) -> None:
    """An earlier run of the unit recorded how it ended; a stop must not relabel its failure."""
    ended_ok, ended_err, live = (_mkrun(host, n) for n in ("run-ok", "run-err", "run-live"))
    (ended_ok / "events.jsonl").write_text(json.dumps({"event": "run_end", "exit_code": 0}) + "\n")
    (ended_err / "events.jsonl").write_text(json.dumps({"event": "run_error", "exit_code": 2}) + "\n")
    (live / "events.jsonl").write_text(json.dumps({"event": "run_start"}) + "\n")
    _register(host, ended_ok, ended_err, live)
    result = _run(host)
    assert result.returncode == 0, result.stderr
    assert observability.read_stop_request(ended_ok) is None
    assert observability.read_stop_request(ended_err) is None
    assert observability.read_stop_request(live) is not None
    assert any("kill" in c for c in _calls(host))


def test_forced_script_records_force_true_before_the_hard_stop(host: dict[str, Path]) -> None:
    a = _mkrun(host, "run-a")
    _register(host, a)
    result = _run(host, force=True)
    assert result.returncode == 0, result.stderr
    rec = observability.read_stop_request(a)
    assert rec is not None
    assert rec["force"] is True
    assert (host["fake"] / "seen.log").read_text().split() == ["present"]
    assert not any("kill" in c for c in _calls(host))


def test_no_registry_still_kills_and_stops(host: dict[str, Path]) -> None:
    result = _run(host)
    assert result.returncode == 0, result.stderr
    assert any("kill" in c for c in _calls(host))
    assert not list(host["root"].glob("**/stop-request.json"))


def test_registry_without_run_dir_gets_no_record(host: dict[str, Path]) -> None:
    (host["runtime"] / f"{UNIT_BASE}.runs").write_text(json.dumps({"run_id": "x", "pid": 1}) + "\n")
    result = _run(host)
    assert result.returncode == 0, result.stderr
    assert any("kill" in c for c in _calls(host))
    assert not list(host["root"].glob("**/stop-request.json"))


def test_missing_run_directory_gets_no_record(host: dict[str, Path]) -> None:
    assert detach_records.append_registration(UNIT_BASE, "r", host["root"] / "gone", 1)
    result = _run(host)
    assert result.returncode == 0, result.stderr
    assert any("kill" in c for c in _calls(host))
    assert not (host["root"] / "gone").exists()


def test_hostile_registry_value_is_not_executed(host: dict[str, Path]) -> None:
    marker = host["root"] / "pwned"
    # A relative marker keeps the name free of slashes; _run runs bash in root.
    hostile = host["root"] / 'ev"il$(touch pwned)\\x'
    hostile.mkdir()
    assert detach_records.append_registration(UNIT_BASE, "r", hostile, 1)
    result = _run(host)
    assert result.returncode == 0, result.stderr
    assert not marker.exists()
    assert observability.read_stop_request(hostile) is None
    assert any("kill" in c for c in _calls(host))


def test_run_directory_with_a_space(host: dict[str, Path]) -> None:
    d = _mkrun(host, "run with space")
    _register(host, d)
    result = _run(host)
    assert result.returncode == 0, result.stderr
    assert observability.read_stop_request(d) is not None
