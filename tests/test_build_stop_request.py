"""stop_run records a stop request only where a signal to a live build follows."""

from __future__ import annotations

import signal
from typing import TYPE_CHECKING

import pytest

from bakar import build_stop, observability, proc_tree, stop_container
from tests.test_build_stop import _make_run_dir

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


def _host_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, live: bool = True) -> Path:
    run_dir = _make_run_dir(tmp_path)
    build_stop.write_launch_record(run_dir, pgid=4242, mode="host")
    monkeypatch.setattr(build_stop, "is_build_running", lambda _rd: (live, 4242 if live else None, live))
    monkeypatch.setattr(proc_tree, "_pgid_alive", lambda _pgid: False)
    monkeypatch.setattr(build_stop.time, "sleep", lambda _s: None)
    return run_dir


def _watch_killpg(monkeypatch: pytest.MonkeyPatch, run_dir: Path) -> list[tuple[int, int, dict | None]]:
    """Patch os.killpg; record each call with the stop record as seen at that moment."""
    calls: list[tuple[int, int, dict | None]] = []

    def fake_killpg(pgid: int, sig: int) -> None:
        calls.append((pgid, sig, observability.read_stop_request(run_dir)))

    monkeypatch.setattr(build_stop.os, "killpg", fake_killpg)
    return calls


def test_live_host_run_has_record_at_first_killpg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_dir = _host_run(tmp_path, monkeypatch)
    calls = _watch_killpg(monkeypatch, run_dir)

    assert build_stop.stop_run(run_dir) is True

    assert calls[0][1] == signal.SIGINT
    record = calls[0][2]
    assert record is not None
    assert record["force"] is False


def test_force_records_force_and_still_escalates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_dir = _host_run(tmp_path, monkeypatch)
    calls = _watch_killpg(monkeypatch, run_dir)

    assert build_stop.stop_run(run_dir, force=True) is True

    assert calls
    assert calls[0][1] == signal.SIGTERM
    assert calls[0][2] is not None
    assert calls[0][2]["force"] is True


def test_no_live_build_leaves_no_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_dir = _host_run(tmp_path, monkeypatch, live=False)
    calls = _watch_killpg(monkeypatch, run_dir)

    assert build_stop.stop_run(run_dir) is True

    assert calls == []
    assert observability.read_stop_request(run_dir) is None
    assert not (run_dir / observability.STOP_REQUEST_FILENAME).exists()


def test_gone_wrapper_with_lingering_cooker_leaves_no_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The run already recorded how it ended; cleaning up its cooker must not relabel it as stopped."""
    run_dir = _host_run(tmp_path, monkeypatch, live=False)
    escalated: list[bool] = []
    monkeypatch.setattr(proc_tree, "_collect_build_pids", lambda _topdir, _extra: type("P", (), {"cooker": [123]})())
    monkeypatch.setattr(proc_tree, "_bitbake_server_alive", lambda _rd: False)
    monkeypatch.setattr(build_stop, "_escalate_host", lambda _pgid, _rd: escalated.append(True))
    monkeypatch.setattr(build_stop, "_report_stale_cleanup", lambda _rd, _cfg: False)
    monkeypatch.setattr(build_stop, "_verify_clean", lambda *_a, **_kw: [])

    assert build_stop.stop_run(run_dir, force=True) is True

    assert escalated == [True]
    assert observability.read_stop_request(run_dir) is None


def test_lock_guard_refusal_leaves_no_record_and_no_signal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_dir = _host_run(tmp_path, monkeypatch)
    calls = _watch_killpg(monkeypatch, run_dir)
    monkeypatch.setattr(build_stop, "lock_mutation_guard", lambda _cfg: object())
    monkeypatch.setattr(build_stop, "_say_stop_refusal", lambda _refusal: None)

    assert build_stop.stop_run(run_dir, cfg=object()) is False  # type: ignore[arg-type]

    assert calls == []
    assert observability.read_stop_request(run_dir) is None


def test_live_container_run_has_record_before_container_stop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_dir = _make_run_dir(tmp_path)
    build_stop.write_launch_record(
        run_dir, pgid=4242, mode="container", runtime="docker", container_label="bakar.run_id=x"
    )
    seen: list[dict | None] = []

    def fake_stop(_runtime: str, _cid: str, _plan: object) -> str:
        seen.append(observability.read_stop_request(run_dir))
        return "stopped"

    ids = iter([(build_stop._ALIVE, "cafef00d"), (build_stop._DEAD, None)])
    monkeypatch.setattr(stop_container, "_container_id_status", lambda _rt, _label: next(ids, (build_stop._DEAD, None)))
    monkeypatch.setattr(build_stop, "_stop_container", fake_stop)
    monkeypatch.setattr(build_stop.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(build_stop.time, "sleep", lambda _s: None)

    assert build_stop.stop_run(run_dir) is True

    assert len(seen) == 1
    assert seen[0] is not None


def test_container_left_running_after_the_run_ended_leaves_no_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run already recorded how it ended; stopping its leftover container must not relabel it."""
    run_dir = _make_run_dir(tmp_path)
    (run_dir / "events.jsonl").write_text('{"event": "run_error", "exit_code": 2}\n')
    build_stop.write_launch_record(
        run_dir, pgid=4242, mode="container", runtime="docker", container_label="bakar.run_id=x"
    )
    seen: list[dict | None] = []

    def fake_stop(_runtime: str, _cid: str, _plan: object) -> str:
        seen.append(observability.read_stop_request(run_dir))
        return "stopped"

    ids = iter([(build_stop._ALIVE, "cafef00d"), (build_stop._DEAD, None)])
    monkeypatch.setattr(stop_container, "_container_id_status", lambda _rt, _label: next(ids, (build_stop._DEAD, None)))
    monkeypatch.setattr(build_stop, "_stop_container", fake_stop)
    monkeypatch.setattr(build_stop.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(build_stop.time, "sleep", lambda _s: None)

    assert build_stop.stop_run(run_dir) is True

    assert seen == [None]
    assert observability.read_stop_request(run_dir) is None


def test_failed_write_does_not_change_result_or_skip_signal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_dir = _host_run(tmp_path, monkeypatch)
    calls = _watch_killpg(monkeypatch, run_dir)
    monkeypatch.setattr(build_stop, "write_stop_request", lambda _rd, *, force=False: False)

    assert build_stop.stop_run(run_dir) is True

    assert [(p, s) for p, s, _ in calls] == [(4242, signal.SIGINT)]
    assert observability.read_stop_request(run_dir) is None
