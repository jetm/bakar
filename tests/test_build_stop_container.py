"""Unit tests for the container-stop primitives in :mod:`bakar.build_stop`.

Covers the graceful container escalation (``_stop_container``), runtime
detection (``_detect_runtime``), container-id resolution (``_container_id``),
and the launch-record round-trip (``write_launch_record`` /
``read_launch_record`` / ``remove_pid``). Every test is hermetic: no real
docker/podman is invoked, ``time.sleep`` is a no-op, and the subprocess seams
are patched on the ``build_stop`` module.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from bakar import build_stop

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


# --- _stop_container escalation order --------------------------------------


def _patch_runtime_seams(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Patch the build_stop runtime seams and return the recorded-argv list.

    ``_run_runtime`` appends each issued argv to the returned list,
    ``_sigint_bitbake_in_container`` records a ``["sigint-bitbake", runtime,
    cid]`` sentinel and reports success (True), ``_container_liveness`` reports
    the container already gone (``_DEAD``) so the unbounded graceful wait drains
    on its first poll, and ``time.sleep`` is a no-op.
    """
    issued: list[list[str]] = []

    def _record(args: list[str]) -> None:
        issued.append(args)

    def _record_sigint(runtime: str, cid: str) -> bool:
        issued.append(["sigint-bitbake", runtime, cid])
        return True

    monkeypatch.setattr(build_stop, "_run_runtime", _record)
    monkeypatch.setattr(build_stop, "_sigint_bitbake_in_container", _record_sigint)
    monkeypatch.setattr(build_stop, "_container_liveness", lambda runtime, cid: build_stop._DEAD)
    monkeypatch.setattr(build_stop.time, "sleep", lambda _s: None)
    return issued


def test_stop_container_graceful_drain_signals_bitbake_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """force=False signals bitbake inside the container, then the wait drains - no SIGKILL.

    The graceful path is now an unbounded liveness wait, not a bounded poll that
    always escalates: once ``_container_liveness`` reports the container gone the
    wait returns without ever running the stop/kill ladder.
    """
    issued = _patch_runtime_seams(monkeypatch)

    status = build_stop._stop_container("docker", "abc123", force=False, term_secs=5)

    assert status == "drained"
    assert issued == [["sigint-bitbake", "docker", "abc123"]]


def test_stop_container_graceful_sigint_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """The very first action in the graceful path is the in-container bitbake SIGINT."""
    issued = _patch_runtime_seams(monkeypatch)

    build_stop._stop_container("podman", "cid", force=False, term_secs=3)

    assert issued[0] == ["sigint-bitbake", "podman", "cid"]


def test_stop_container_force_skips_sigint(monkeypatch: pytest.MonkeyPatch) -> None:
    """force=True signals no bitbake SIGINT; it runs the stop -> SIGKILL ladder."""
    issued = _patch_runtime_seams(monkeypatch)

    status = build_stop._stop_container("docker", "abc", force=True, term_secs=7)

    assert status == "forced"
    assert not any(call[0] == "sigint-bitbake" for call in issued)
    assert not any("SIGINT" in arg for call in issued for arg in call)
    assert issued == [
        ["docker", "stop", "--timeout=7", "abc"],
        ["docker", "kill", "--signal=SIGKILL", "abc"],
    ]


def test_stop_container_falls_back_to_pid1_sigint_when_exec_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the in-container SIGINT exec fails, fall back to a container-PID-1 SIGINT."""
    issued: list[list[str]] = []
    monkeypatch.setattr(build_stop, "_run_runtime", issued.append)
    monkeypatch.setattr(build_stop, "_sigint_bitbake_in_container", lambda runtime, cid: False)
    monkeypatch.setattr(build_stop, "_container_liveness", lambda runtime, cid: build_stop._DEAD)
    monkeypatch.setattr(build_stop.time, "sleep", lambda _s: None)

    build_stop._stop_container("docker", "abc", force=False, term_secs=4)

    assert issued[0] == ["docker", "kill", "--signal=SIGINT", "abc"]


def test_stop_container_ctrl_c_reaches_stop_kill_ladder(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Ctrl-C during the graceful wait escalates to ``stop --timeout`` -> SIGKILL.

    The graceful drain path never reaches the 5s ``stop --timeout`` step (that is
    asserted by ``test_stop_container_graceful_drain_signals_bitbake_only``); the
    timeout ladder is reached only when the operator interrupts the wait. A
    ``_container_liveness`` poll raising ``KeyboardInterrupt`` stands in for the
    Ctrl-C, so the real ``_graceful_wait`` catches it on the first poll (no sleep)
    and fires the container escalate ladder.
    """
    issued: list[list[str]] = []
    monkeypatch.setattr(build_stop, "_run_runtime", issued.append)
    monkeypatch.setattr(build_stop, "_sigint_bitbake_in_container", lambda runtime, cid: True)

    def _ctrl_c(_runtime: str, _cid: str) -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr(build_stop, "_container_liveness", _ctrl_c)

    status = build_stop._stop_container("docker", "abc", force=False, term_secs=5)

    assert status == "escalated"
    assert issued == [
        ["docker", "stop", "--timeout=5", "abc"],
        ["docker", "kill", "--signal=SIGKILL", "abc"],
    ]


def test_stop_container_lost_runtime_propagates_without_ladder(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A ``lost_runtime`` graceful wait is surfaced verbatim, without escalation.

    When the runtime goes unreachable mid-wait the graceful wait returns
    ``lost_runtime`` (the consecutive-query-error cap is exercised directly in
    ``tests/test_build_stop.py``). ``_stop_container`` must propagate that status,
    print the "lost contact" warning, and NOT run the SIGTERM->SIGKILL ladder so
    the caller can map it to exit 1.
    """
    issued: list[list[str]] = []
    monkeypatch.setattr(build_stop, "_run_runtime", issued.append)
    monkeypatch.setattr(build_stop, "_sigint_bitbake_in_container", lambda runtime, cid: True)
    monkeypatch.setattr(build_stop, "_graceful_wait", lambda **_kwargs: "lost_runtime")

    status = build_stop._stop_container("podman", "cid", force=False, term_secs=5)

    assert status == "lost_runtime"
    assert issued == []  # the SIGTERM->SIGKILL ladder never ran
    assert "lost contact with the container runtime" in capsys.readouterr().out


# --- _detect_runtime -------------------------------------------------------


def test_detect_runtime_honors_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """KAS_CONTAINER_ENGINE=podman selects podman regardless of PATH."""
    monkeypatch.setenv("KAS_CONTAINER_ENGINE", "podman")
    monkeypatch.setattr(build_stop.shutil, "which", lambda _name: "/usr/bin/docker")

    assert build_stop._detect_runtime() == "podman"


def test_detect_runtime_prefers_docker_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no env override and docker on PATH, docker wins."""
    monkeypatch.delenv("KAS_CONTAINER_ENGINE", raising=False)
    monkeypatch.setattr(build_stop.shutil, "which", lambda name: "/usr/bin/docker" if name == "docker" else None)

    assert build_stop._detect_runtime() == "docker"


def test_detect_runtime_falls_back_to_podman(monkeypatch: pytest.MonkeyPatch) -> None:
    """With only podman installed, podman is detected."""
    monkeypatch.delenv("KAS_CONTAINER_ENGINE", raising=False)
    monkeypatch.setattr(build_stop.shutil, "which", lambda name: "/usr/bin/podman" if name == "podman" else None)

    assert build_stop._detect_runtime() == "podman"


# --- _container_id ---------------------------------------------------------


def test_container_id_empty_stdout_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty `ps -q -f label=` stdout resolves to no container."""
    monkeypatch.setattr(
        build_stop.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    assert build_stop._container_id("docker", "bakar.run_id=X") is None


def test_container_id_returns_first_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """A container id line is returned verbatim."""
    monkeypatch.setattr(
        build_stop.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="deadbeef1234\n", stderr=""),
    )

    assert build_stop._container_id("podman", "bakar.run_id=X") == "deadbeef1234"


# --- launch record round-trip ----------------------------------------------


def test_launch_record_container_round_trip(tmp_path: Path) -> None:
    """A container record round-trips and writes both sidecar files."""
    build_stop.write_launch_record(
        tmp_path,
        pgid=4242,
        mode="container",
        runtime="docker",
        container_label="bakar.run_id=X",
    )

    record = build_stop.read_launch_record(tmp_path)
    assert record.pgid == 4242
    assert record.mode == "container"
    assert record.runtime == "docker"
    assert record.container_label == "bakar.run_id=X"
    assert (tmp_path / "build.meta.json").exists()
    assert (tmp_path / "build.pid").exists()


def test_launch_record_host_round_trip(tmp_path: Path) -> None:
    """A host record round-trips with runtime and label both None."""
    build_stop.write_launch_record(tmp_path, pgid=99, mode="host")

    record = build_stop.read_launch_record(tmp_path)
    assert record.pgid == 99
    assert record.mode == "host"
    assert record.runtime is None
    assert record.container_label is None


def test_read_launch_record_legacy_run(tmp_path: Path) -> None:
    """A legacy run (build.pid only) reads as container mode with no label."""
    build_stop.write_pid(tmp_path, 1234)
    assert not (tmp_path / "build.meta.json").exists()

    record = build_stop.read_launch_record(tmp_path)
    assert record.pgid == 1234
    assert record.mode == "container"
    assert record.container_label is None


def test_remove_pid_clears_both_sidecars(tmp_path: Path) -> None:
    """remove_pid unlinks both build.pid and build.meta.json."""
    build_stop.write_launch_record(
        tmp_path,
        pgid=7,
        mode="container",
        runtime="podman",
        container_label="bakar.run_id=Y",
    )
    assert (tmp_path / "build.pid").exists()
    assert (tmp_path / "build.meta.json").exists()

    build_stop.remove_pid(tmp_path)

    assert not (tmp_path / "build.pid").exists()
    assert not (tmp_path / "build.meta.json").exists()


# --- stop_running_proc (in-process Ctrl-C / stall watchdog) ----------------


def test_stop_running_proc_container_sends_single_sigint(monkeypatch: pytest.MonkeyPatch) -> None:
    """Container mode SIGINTs bitbake INSIDE the container once and neither runs
    the blocking escalation (_stop_container) nor signals the wrapper PGID."""
    sigint_calls: list[tuple[str, str]] = []
    stop_container_calls: list[object] = []
    killpg_calls: list[tuple[int, int]] = []
    monkeypatch.setattr(build_stop, "detect_runtime", lambda: "docker")
    monkeypatch.setattr(build_stop, "_container_id", lambda runtime, label: "abc123")
    monkeypatch.setattr(
        build_stop,
        "_sigint_bitbake_in_container",
        lambda runtime, cid: bool(sigint_calls.append((runtime, cid))) or True,
    )
    monkeypatch.setattr(build_stop, "_stop_container", lambda *a, **k: stop_container_calls.append((a, k)))
    monkeypatch.setattr(build_stop.os, "killpg", lambda pgid, sig: killpg_calls.append((pgid, sig)))

    proc = SimpleNamespace(pid=4242)
    cfg = SimpleNamespace(host_mode=False)
    log = SimpleNamespace(run_id="20260101-000000")
    build_stop.stop_running_proc(proc, cfg, log)  # type: ignore[arg-type]

    assert sigint_calls == [("docker", "abc123")]  # bitbake signalled inside the container
    assert stop_container_calls == []  # must not run the blocking grace/escalation loop
    assert killpg_calls == []  # wrapper PGID not signalled once the container resolves


def test_stop_running_proc_container_exec_fail_falls_back_to_pgid(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the in-container SIGINT exec fails, fall back to os.killpg(SIGINT)."""
    killpg_calls: list[tuple[int, int]] = []
    monkeypatch.setattr(build_stop, "detect_runtime", lambda: "docker")
    monkeypatch.setattr(build_stop, "_container_id", lambda runtime, label: "abc123")
    monkeypatch.setattr(build_stop, "_sigint_bitbake_in_container", lambda runtime, cid: False)
    monkeypatch.setattr(build_stop.os, "killpg", lambda pgid, sig: killpg_calls.append((pgid, sig)))

    proc = SimpleNamespace(pid=4242)
    cfg = SimpleNamespace(host_mode=False)
    log = SimpleNamespace(run_id="20260101-000000")
    build_stop.stop_running_proc(proc, cfg, log)  # type: ignore[arg-type]

    assert killpg_calls == [(4242, build_stop.signal.SIGINT)]


def test_stop_running_proc_container_falls_back_to_pgid(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the container cannot be resolved, fall back to os.killpg(SIGINT)."""
    issued: list[list[str]] = []
    killpg_calls: list[tuple[int, int]] = []
    monkeypatch.setattr(build_stop, "_run_runtime", issued.append)
    monkeypatch.setattr(build_stop, "detect_runtime", lambda: "docker")
    monkeypatch.setattr(build_stop, "_container_id", lambda runtime, label: None)
    monkeypatch.setattr(build_stop.os, "killpg", lambda pgid, sig: killpg_calls.append((pgid, sig)))

    proc = SimpleNamespace(pid=4242)
    cfg = SimpleNamespace(host_mode=False)
    log = SimpleNamespace(run_id="20260101-000000")
    build_stop.stop_running_proc(proc, cfg, log)  # type: ignore[arg-type]

    assert issued == []  # no runtime call when there is no container to signal
    assert killpg_calls == [(4242, build_stop.signal.SIGINT)]


def test_stop_running_proc_host_signals_pgid(monkeypatch: pytest.MonkeyPatch) -> None:
    """Host mode signals the wrapper PGID with SIGINT and makes no runtime call."""
    issued: list[list[str]] = []
    killpg_calls: list[tuple[int, int]] = []
    monkeypatch.setattr(build_stop, "_run_runtime", issued.append)
    monkeypatch.setattr(build_stop.os, "killpg", lambda pgid, sig: killpg_calls.append((pgid, sig)))

    proc = SimpleNamespace(pid=999)
    cfg = SimpleNamespace(host_mode=True)
    log = SimpleNamespace(run_id="20260101-000000")
    build_stop.stop_running_proc(proc, cfg, log)  # type: ignore[arg-type]

    assert issued == []
    assert killpg_calls == [(999, build_stop.signal.SIGINT)]


# --- _sigint_bitbake_in_container ------------------------------------------


def test_sigint_bitbake_in_container_execs_pkill_main_bitbake(monkeypatch: pytest.MonkeyPatch) -> None:
    """It execs `pkill -INT -f 'bin/bitbake '` (trailing space targets the UI, not workers)."""
    calls: list[list[str]] = []

    def _fake_run(args: list[str], **_kwargs: object) -> SimpleNamespace:
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(build_stop.subprocess, "run", _fake_run)

    assert build_stop._sigint_bitbake_in_container("docker", "abc123") is True
    assert calls == [["docker", "exec", "abc123", "pkill", "-INT", "-f", "bin/bitbake "]]


def test_sigint_bitbake_in_container_no_match_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """pkill exit 1 (no bitbake process matched) -> False so the caller can fall back."""
    monkeypatch.setattr(
        build_stop.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr=""),
    )

    assert build_stop._sigint_bitbake_in_container("podman", "cid") is False


def test_sigint_bitbake_in_container_oserror_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """An OSError from the exec (runtime binary absent) -> False, never raises."""

    def _boom(*_a: object, **_k: object) -> SimpleNamespace:
        raise OSError("no runtime")

    monkeypatch.setattr(build_stop.subprocess, "run", _boom)

    assert build_stop._sigint_bitbake_in_container("docker", "abc") is False
