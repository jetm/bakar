"""Unit tests for bakar.build_stop lifecycle helpers.

Covers the pidfile round-trip, the procfs-cmdline liveness probe in
``is_build_running``, the mode-aware ``stop_build`` dispatch (host PGID
SIGINT->grace->SIGTERM->SIGKILL escalation, container runtime stop, and the
legacy/untargetable run that signals nothing), and the unclean-stop scan in
``check_unclean_stop``. Every test is hermetic: no signals reach real
processes, ``time.sleep`` is monkeypatched to a no-op, and ``os.killpg``,
``_container_id``, and ``_stop_container`` are recorded rather than executed.
"""

from __future__ import annotations

import json
import os
import signal
from pathlib import Path

import pytest
from rich.console import Console

from bakar import build_stop

pytestmark = pytest.mark.unit


def _make_run_dir(bsp_root: Path, run_id: str = "20260618-120000") -> Path:
    """Create ``bsp_root/build/runs/<run_id>`` and return it."""
    run_dir = bsp_root / "build" / "runs" / run_id
    run_dir.mkdir(parents=True)
    return run_dir


# --- write_pid / remove_pid round-trip -------------------------------------


def test_write_pid_creates_decimal_pidfile(tmp_path: Path) -> None:
    """write_pid writes the decimal PGID followed by a newline."""
    build_stop.write_pid(tmp_path, 4242)

    pid_file = tmp_path / "build.pid"
    assert pid_file.exists()
    assert pid_file.read_text() == "4242\n"


def test_remove_pid_deletes_existing_file(tmp_path: Path) -> None:
    """remove_pid unlinks an existing build.pid."""
    build_stop.write_pid(tmp_path, 99)
    assert (tmp_path / "build.pid").exists()

    build_stop.remove_pid(tmp_path)

    assert not (tmp_path / "build.pid").exists()


def test_remove_pid_absent_is_noop(tmp_path: Path) -> None:
    """remove_pid on a missing build.pid does not raise."""
    build_stop.remove_pid(tmp_path)  # no file present
    assert not (tmp_path / "build.pid").exists()


# --- is_build_running -------------------------------------------------------


def test_is_build_running_live_pgid_wrong_cmdline(tmp_path: Path) -> None:
    """A live PGID (this test's process group) is live but its cmdline lacks kas tokens.

    The pytest process-group leader's cmdline contains ``python``/``pytest``,
    never ``kas-container`` or ``kas``, so cmdline_ok must be False even though
    the group is unmistakably alive.
    """
    pgid = os.getpgrp()
    build_stop.write_pid(tmp_path, pgid)

    live, pgid_out, cmdline_ok = build_stop.is_build_running(tmp_path)

    assert live is True
    assert pgid_out == pgid
    assert cmdline_ok is False


def test_is_build_running_dead_pid(tmp_path: Path) -> None:
    """A PID that does not exist reports live=False."""
    build_stop.write_pid(tmp_path, 9999999)

    live, pgid, cmdline_ok = build_stop.is_build_running(tmp_path)

    assert live is False
    assert pgid == 9999999
    assert cmdline_ok is False


def test_is_build_running_missing_pidfile(tmp_path: Path) -> None:
    """No build.pid -> not running, no pgid."""
    live, pgid, cmdline_ok = build_stop.is_build_running(tmp_path)

    assert live is False
    assert pgid is None
    assert cmdline_ok is False


def test_is_build_running_cmdline_ok_true(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live PGID whose /proc cmdline contains kas-container -> cmdline_ok True.

    The module reads the cmdline via ``Path.read_bytes()`` on
    ``/proc/<pgid>/cmdline``. Patch that exact seam: return a fake
    null-separated cmdline for the procfs path and defer to the real
    implementation for every other path so the pidfile read still works.
    """
    pgid = os.getpgrp()
    build_stop.write_pid(tmp_path, pgid)
    proc_cmdline = Path(f"/proc/{pgid}/cmdline")
    real_read_bytes = Path.read_bytes

    def fake_read_bytes(self: Path) -> bytes:
        if self == proc_cmdline:
            return b"kas-container\x00build\x00"
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", fake_read_bytes)

    live, pgid_out, cmdline_ok = build_stop.is_build_running(tmp_path)

    assert live is True
    assert pgid_out == pgid
    assert cmdline_ok is True


# --- stop_build -------------------------------------------------------------


def _record_killpg(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int]]:
    """Patch os.killpg to record (pgid, sig) calls instead of signalling."""
    calls: list[tuple[int, int]] = []

    def fake_killpg(pgid: int, sig: int) -> None:
        calls.append((pgid, sig))

    monkeypatch.setattr(build_stop.os, "killpg", fake_killpg)
    return calls


def test_stop_build_sigint_then_clean_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Host-mode force=False sends SIGINT; a quick PGID death stops escalation.

    The run is set up as a host launch record so stop_build takes the PGID path.
    is_build_running is stubbed to report a live, verified build. _pgid_alive
    returns False on the first grace poll so the loop exits before any
    escalation. remove_pid must run regardless.
    """
    run_dir = _make_run_dir(tmp_path)
    build_stop.write_launch_record(run_dir, pgid=4242, mode="host")

    monkeypatch.setattr(build_stop, "is_build_running", lambda _rd: (True, 4242, True))
    monkeypatch.setattr(build_stop, "_pgid_alive", lambda _pgid: False)
    monkeypatch.setattr(build_stop.time, "sleep", lambda _s: None)
    calls = _record_killpg(monkeypatch)

    assert build_stop.stop_build(tmp_path) is True

    assert calls == [(4242, signal.SIGINT)]
    assert not (run_dir / "build.pid").exists()
    assert not (run_dir / "build.meta.json").exists()


def test_stop_build_escalates_through_sigterm_sigkill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Host-mode force=False escalates SIGINT -> SIGTERM -> SIGKILL when the PGID lingers.

    _pgid_alive stays True across the grace loop and the post-SIGTERM check,
    forcing the full escalation ladder. Grace is shrunk and sleep neutered to
    keep the test instant.
    """
    run_dir = _make_run_dir(tmp_path)
    build_stop.write_launch_record(run_dir, pgid=4242, mode="host")

    monkeypatch.setattr(build_stop, "is_build_running", lambda _rd: (True, 4242, True))
    monkeypatch.setattr(build_stop, "_pgid_alive", lambda _pgid: True)
    monkeypatch.setattr(build_stop, "_STOP_GRACE_SECONDS", 2)
    monkeypatch.setattr(build_stop.time, "sleep", lambda _s: None)
    calls = _record_killpg(monkeypatch)

    assert build_stop.stop_build(tmp_path) is True

    sigs = [sig for _pgid, sig in calls]
    assert sigs == [signal.SIGINT, signal.SIGTERM, signal.SIGKILL]
    assert all(pgid == 4242 for pgid, _sig in calls)
    assert not (run_dir / "build.pid").exists()
    assert not (run_dir / "build.meta.json").exists()


def test_stop_build_force_skips_sigint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Host-mode force=True sends SIGTERM first and never sends SIGINT."""
    run_dir = _make_run_dir(tmp_path)
    build_stop.write_launch_record(run_dir, pgid=4242, mode="host")

    monkeypatch.setattr(build_stop, "is_build_running", lambda _rd: (True, 4242, True))
    monkeypatch.setattr(build_stop, "_pgid_alive", lambda _pgid: False)
    monkeypatch.setattr(build_stop.time, "sleep", lambda _s: None)
    calls = _record_killpg(monkeypatch)

    assert build_stop.stop_build(tmp_path, force=True) is True

    sigs = [sig for _pgid, sig in calls]
    assert signal.SIGINT not in sigs
    assert sigs[0] == signal.SIGTERM
    assert not (run_dir / "build.pid").exists()
    assert not (run_dir / "build.meta.json").exists()


def test_stop_build_no_running_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host record with a dead/unverified build means no signal is ever sent."""
    run_dir = _make_run_dir(tmp_path)
    build_stop.write_launch_record(run_dir, pgid=4242, mode="host")

    monkeypatch.setattr(build_stop, "is_build_running", lambda _rd: (False, None, False))
    monkeypatch.setattr(build_stop.time, "sleep", lambda _s: None)
    calls = _record_killpg(monkeypatch)

    assert build_stop.stop_build(tmp_path) is False

    assert calls == []
    assert not (run_dir / "build.pid").exists()
    assert not (run_dir / "build.meta.json").exists()


# --- mode-aware stop_build branching ---------------------------------------


def test_stop_build_container_mode_stops_container_not_pgid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A container record resolves+stops the container and never touches the PGID."""
    run_dir = _make_run_dir(tmp_path)
    build_stop.write_launch_record(
        run_dir,
        pgid=4242,
        mode="container",
        runtime="docker",
        container_label="bakar.run_id=20260618-120000",
    )

    stop_calls: list[tuple[str, str]] = []

    monkeypatch.setattr(build_stop, "_container_id", lambda _rt, _label: "cafef00d")
    monkeypatch.setattr(
        build_stop,
        "_stop_container",
        lambda runtime, cid, **_kw: stop_calls.append((runtime, cid)),
    )
    monkeypatch.setattr(build_stop.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(build_stop.time, "sleep", lambda _s: None)
    calls = _record_killpg(monkeypatch)

    assert build_stop.stop_build(tmp_path) is True

    assert stop_calls == [("docker", "cafef00d")]
    assert calls == []
    assert not (run_dir / "build.pid").exists()
    assert not (run_dir / "build.meta.json").exists()


def test_stop_build_host_mode_signals_pgid_not_container(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host record signals the PGID with SIGINT and never stops a container."""
    run_dir = _make_run_dir(tmp_path)
    build_stop.write_launch_record(run_dir, pgid=4242, mode="host")

    stop_calls: list[tuple[str, str]] = []

    monkeypatch.setattr(build_stop, "is_build_running", lambda _rd: (True, 4242, True))
    monkeypatch.setattr(build_stop, "_pgid_alive", lambda _pgid: False)
    monkeypatch.setattr(
        build_stop,
        "_stop_container",
        lambda runtime, cid, **_kw: stop_calls.append((runtime, cid)),
    )
    monkeypatch.setattr(build_stop.time, "sleep", lambda _s: None)
    calls = _record_killpg(monkeypatch)

    assert build_stop.stop_build(tmp_path) is True

    assert calls == [(4242, signal.SIGINT)]
    assert stop_calls == []
    assert not (run_dir / "build.pid").exists()
    assert not (run_dir / "build.meta.json").exists()


def test_stop_build_legacy_run_targets_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A legacy run (build.pid only, no build.meta.json) cannot be targeted.

    read_launch_record classifies a bare pidfile as a container run with no
    label, so stop_build must signal neither the wrapper PGID nor a container,
    return False, and print no "stopped" success line.
    """
    run_dir = _make_run_dir(tmp_path)
    build_stop.write_pid(run_dir, 4242)  # legacy: no build.meta.json

    stop_calls: list[tuple[str, str]] = []

    monkeypatch.setattr(
        build_stop,
        "_stop_container",
        lambda runtime, cid, **_kw: stop_calls.append((runtime, cid)),
    )
    monkeypatch.setattr(build_stop.time, "sleep", lambda _s: None)
    calls = _record_killpg(monkeypatch)

    assert build_stop.stop_build(tmp_path) is False

    assert calls == []
    assert stop_calls == []
    assert "stopped" not in capsys.readouterr().out
    assert not (run_dir / "build.pid").exists()
    assert not (run_dir / "build.meta.json").exists()


def test_stop_build_container_id_unresolved_returns_false(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A container record whose label resolves to no live container stops nothing."""
    run_dir = _make_run_dir(tmp_path)
    build_stop.write_launch_record(
        run_dir,
        pgid=4242,
        mode="container",
        runtime="docker",
        container_label="bakar.run_id=20260618-120000",
    )

    stop_calls: list[tuple[str, str]] = []

    monkeypatch.setattr(build_stop, "_container_id", lambda _rt, _label: None)
    monkeypatch.setattr(
        build_stop,
        "_stop_container",
        lambda runtime, cid, **_kw: stop_calls.append((runtime, cid)),
    )
    monkeypatch.setattr(build_stop.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(build_stop.time, "sleep", lambda _s: None)
    calls = _record_killpg(monkeypatch)

    assert build_stop.stop_build(tmp_path) is False

    assert stop_calls == []
    assert calls == []
    assert not (run_dir / "build.pid").exists()
    assert not (run_dir / "build.meta.json").exists()


# --- check_unclean_stop -----------------------------------------------------


def test_check_unclean_stop_stale_names_interrupted_step(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale build.pid with an unmatched step_start warns and names the step."""
    run_dir = _make_run_dir(tmp_path)
    build_stop.write_pid(run_dir, 9999999)  # dead PGID -> stale
    events = run_dir / "events.jsonl"
    events.write_text(json.dumps({"event": "step_start", "step": "kas_build"}) + "\n")

    # Dead PGID: is_build_running already reports live=False for 9999999, but
    # pin it explicitly so the test does not depend on PID 9999999 being unused.
    monkeypatch.setattr(build_stop, "is_build_running", lambda _rd: (False, 9999999, False))
    console = Console(record=True, width=100)

    build_stop.check_unclean_stop(tmp_path, console)

    output = console.export_text()
    assert "interrupted uncleanly" in output
    assert "kas_build" in output


def test_check_unclean_stop_no_pidfile_silent(tmp_path: Path) -> None:
    """An empty runs dir (no build.pid) prints nothing."""
    _make_run_dir(tmp_path)  # run dir exists but holds no build.pid
    console = Console(record=True, width=100)

    build_stop.check_unclean_stop(tmp_path, console)

    assert console.export_text().strip() == ""
