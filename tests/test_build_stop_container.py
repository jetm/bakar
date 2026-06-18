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
    ``_container_running`` always reports still-running so the escalation runs
    to completion, and ``time.sleep`` is a no-op to keep the grace loop fast.
    """
    issued: list[list[str]] = []

    def _record(args: list[str]) -> None:
        issued.append(args)

    monkeypatch.setattr(build_stop, "_run_runtime", _record)
    monkeypatch.setattr(build_stop, "_container_running", lambda runtime, cid: True)
    monkeypatch.setattr(build_stop.time, "sleep", lambda _s: None)
    return issued


def test_stop_container_graceful_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """force=False issues SIGINT first, then stop --timeout, then SIGKILL."""
    issued = _patch_runtime_seams(monkeypatch)

    build_stop._stop_container("docker", "abc123", force=False, grace_secs=2, term_secs=5)

    assert issued == [
        ["docker", "kill", "--signal=SIGINT", "abc123"],
        ["docker", "stop", "--timeout=5", "abc123"],
        ["docker", "kill", "--signal=SIGKILL", "abc123"],
    ]


def test_stop_container_graceful_sigint_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """The very first runtime call in the graceful path is the SIGINT kill."""
    issued = _patch_runtime_seams(monkeypatch)

    build_stop._stop_container("podman", "cid", force=False, grace_secs=1, term_secs=3)

    assert issued[0] == ["podman", "kill", "--signal=SIGINT", "cid"]


def test_stop_container_force_skips_sigint(monkeypatch: pytest.MonkeyPatch) -> None:
    """force=True issues no SIGINT kill; the first call is stop --timeout."""
    issued = _patch_runtime_seams(monkeypatch)

    build_stop._stop_container("docker", "abc", force=True, grace_secs=2, term_secs=7)

    assert ["docker", "kill", "--signal=SIGINT", "abc"] not in issued
    assert not any("SIGINT" in arg for call in issued for arg in call)
    assert issued[0] == ["docker", "stop", "--timeout=7", "abc"]


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
    """Container mode sends ONE non-blocking container SIGINT and neither runs
    the blocking escalation (_stop_container) nor signals the wrapper PGID."""
    issued: list[list[str]] = []
    stop_container_calls: list[object] = []
    killpg_calls: list[tuple[int, int]] = []
    monkeypatch.setattr(build_stop, "_run_runtime", issued.append)
    monkeypatch.setattr(build_stop, "_detect_runtime", lambda: "docker")
    monkeypatch.setattr(build_stop, "_container_id", lambda runtime, label: "abc123")
    monkeypatch.setattr(build_stop, "_stop_container", lambda *a, **k: stop_container_calls.append((a, k)))
    monkeypatch.setattr(build_stop.os, "killpg", lambda pgid, sig: killpg_calls.append((pgid, sig)))

    proc = SimpleNamespace(pid=4242)
    cfg = SimpleNamespace(host_mode=False)
    log = SimpleNamespace(run_id="20260101-000000")
    build_stop.stop_running_proc(proc, cfg, log)  # type: ignore[arg-type]

    assert issued == [["docker", "kill", "--signal=SIGINT", "abc123"]]
    assert stop_container_calls == []  # must not run the blocking grace/escalation loop
    assert killpg_calls == []  # wrapper PGID not signalled once the container resolves


def test_stop_running_proc_container_falls_back_to_pgid(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the container cannot be resolved, fall back to os.killpg(SIGINT)."""
    issued: list[list[str]] = []
    killpg_calls: list[tuple[int, int]] = []
    monkeypatch.setattr(build_stop, "_run_runtime", issued.append)
    monkeypatch.setattr(build_stop, "_detect_runtime", lambda: "docker")
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
