"""Unit tests for bakar.hashserv pure helpers and is_running predicate.

Covers the deterministic port derivation, the workspace-pinned binary
lookup (which explicitly must NOT fall through to host PATH), and the
PID-file + cmdline-based liveness probe in ``is_running``. The cmdline
check is what guards against PID recycling - a stale PID file pointing
at a now-unrelated process must not look "running" to the rest of the
pipeline.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from bakar.hashserv import _find_binary, _workspace_port, is_running

pytestmark = pytest.mark.unit


def test_workspace_port_is_deterministic(tmp_path: Path) -> None:
    """Same workspace path must always derive the same port."""
    assert _workspace_port(tmp_path) == _workspace_port(tmp_path)


def test_workspace_port_in_ephemeral_range(tmp_path: Path) -> None:
    """Derived port must fall in the IANA ephemeral range (49152-65534)."""
    port = _workspace_port(tmp_path)
    assert 49152 <= port < 65535


def test_workspace_port_differs_across_paths(tmp_path: Path) -> None:
    """Two distinct workspace paths should generally yield different ports.

    Collision probability is ~1-in-16383; two adjacent tmp_path siblings
    are independent SHA-256 inputs so a collision here would be a real
    bug, not just bad luck.
    """
    workspace_a = tmp_path / "wsA"
    workspace_b = tmp_path / "wsB"
    workspace_a.mkdir()
    workspace_b.mkdir()
    assert _workspace_port(workspace_a) != _workspace_port(workspace_b)


def test_find_binary_workspace_hit(tmp_path: Path) -> None:
    """When the workspace binary exists, its path is returned."""
    binary_path = tmp_path / "sources" / "poky" / "bitbake" / "bin" / "bitbake-hashserv"
    binary_path.parent.mkdir(parents=True)
    binary_path.write_text("#!/bin/sh\n")
    binary_path.chmod(0o755)

    result = _find_binary(tmp_path)

    assert result == binary_path


def test_find_binary_returns_none_when_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the workspace binary is absent, return None - never PATH.

    Booby-trap ``shutil.which`` so any host-PATH fallback raises; the
    spec pins the workspace path and a PATH-mismatch daemon would speak
    a different protocol than the workspace bitbake.
    """

    def _explode(*_args: object, **_kwargs: object) -> None:
        msg = "shutil.which must not be consulted - hashserv is workspace-pinned"
        raise AssertionError(msg)

    monkeypatch.setattr(shutil, "which", _explode)

    result = _find_binary(tmp_path)

    assert result is None


def _write_pid_file(workspace: Path, pid: int) -> Path:
    """Helper: write ``<workspace>/.bakar/hashserv.pid`` with ``pid``."""
    state_dir = workspace / ".bakar"
    state_dir.mkdir(parents=True, exist_ok=True)
    pid_file = state_dir / "hashserv.pid"
    pid_file.write_text(f"{pid}\n")
    return pid_file


def test_is_running_pid_file_absent(tmp_path: Path) -> None:
    """No PID file under .bakar/ - the daemon cannot be running."""
    assert is_running(tmp_path) is False


def test_is_running_pid_dead(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PID file exists, but the PID itself is gone (ProcessLookupError)."""
    _write_pid_file(tmp_path, 999999)

    def _kill_raises(_pid: int, _sig: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(os, "kill", _kill_raises)

    assert is_running(tmp_path) is False


def test_is_running_pid_recycled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PID is alive but its cmdline does not mention bitbake-hashserv.

    Simulates the post-reboot scenario where the recorded PID has been
    handed to an unrelated process (here, a shell). Without the cmdline
    guard, ``is_running`` would mistakenly report True.
    """
    _write_pid_file(tmp_path, 12345)

    monkeypatch.setattr(os, "kill", lambda _pid, _sig: None)

    real_read_bytes = Path.read_bytes

    def _fake_read_bytes(self: Path) -> bytes:
        if str(self).startswith("/proc/"):
            # NUL-separated argv for a shell process - no bitbake-hashserv.
            return b"/bin/bash\x00-l\x00"
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", _fake_read_bytes)

    assert is_running(tmp_path) is False


def test_is_running_pid_alive_correct_cmdline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PID alive AND its cmdline contains bitbake-hashserv - True."""
    _write_pid_file(tmp_path, 12345)

    monkeypatch.setattr(os, "kill", lambda _pid, _sig: None)

    real_read_bytes = Path.read_bytes

    def _fake_read_bytes(self: Path) -> bytes:
        if str(self).startswith("/proc/"):
            return (
                b"/usr/bin/python3\x00"
                b"/workspace/sources/poky/bitbake/bin/bitbake-hashserv\x00"
                b"--bind\x00ws://localhost:50000\x00"
            )
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", _fake_read_bytes)

    assert is_running(tmp_path) is True


class _FakeProc:
    """Stand-in for ``subprocess.Popen`` with deterministic poll/stderr behavior.

    Drives ``ensure_running``'s probe loop and abort path without spawning
    a real process. ``poll_returns`` is an iterable of values returned by
    successive ``poll()`` calls; the last value sticks once consumed.
    """

    def __init__(
        self,
        pid: int = 12345,
        poll_returns: list[int | None] | None = None,
        stderr_bytes: bytes = b"",
    ) -> None:
        self.pid = pid
        self._poll_returns = list(poll_returns) if poll_returns is not None else [None]
        self._poll_calls = 0

        class _Reader:
            def __init__(self, data: bytes) -> None:
                self._data = data
                self.read_calls = 0

            def read(self) -> bytes:
                self.read_calls += 1
                return self._data

        self.stderr = _Reader(stderr_bytes)

    def poll(self) -> int | None:
        if self._poll_calls < len(self._poll_returns):
            value = self._poll_returns[self._poll_calls]
        else:
            value = self._poll_returns[-1]
        self._poll_calls += 1
        return value


class _FakeSocket:
    """Stand-in for the socket returned by ``socket.create_connection``."""

    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


def _create_workspace_binary(workspace: Path) -> Path:
    """Touch a fake bitbake-hashserv under the workspace and return its path."""
    binary = workspace / "sources" / "poky" / "bitbake" / "bin" / "bitbake-hashserv"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    return binary


def test_ensure_running_returns_none_when_binary_missing(tmp_path: Path) -> None:
    """No workspace binary - return None and write no state files."""
    from bakar.hashserv import ensure_running

    assert ensure_running(tmp_path, binary_root=tmp_path) is None
    state_dir = tmp_path / ".bakar"
    if state_dir.exists():
        assert not (state_dir / "hashserv.pid").exists()
        assert not (state_dir / "hashserv.port").exists()


def test_ensure_running_returns_existing_when_alive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Daemon already running - return the recorded URL without spawning."""
    from bakar import hashserv as hashserv_mod

    state_dir = tmp_path / ".bakar"
    state_dir.mkdir(parents=True)
    (state_dir / "hashserv.port").write_text("54321\n")

    monkeypatch.setattr(hashserv_mod, "is_running", lambda _root: True)

    def _popen_explodes(*_args: object, **_kwargs: object) -> None:
        msg = "subprocess.Popen must not be called when daemon is already running"
        raise AssertionError(msg)

    monkeypatch.setattr(hashserv_mod.subprocess, "Popen", _popen_explodes)

    assert hashserv_mod.ensure_running(tmp_path, binary_root=tmp_path) == "ws://localhost:54321"


def test_ensure_running_starts_process_and_probe_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fresh spawn, TCP probe succeeds: write PID + port files, return URL."""
    from bakar import hashserv as hashserv_mod

    _create_workspace_binary(tmp_path)
    monkeypatch.setattr(hashserv_mod, "is_running", lambda _root: False)

    fake_proc = _FakeProc(pid=12345, poll_returns=[None])
    captured_popen_args: dict[str, object] = {}

    def _fake_popen(args: list[str], **kwargs: object) -> _FakeProc:
        captured_popen_args["args"] = args
        captured_popen_args["kwargs"] = kwargs
        return fake_proc

    monkeypatch.setattr(hashserv_mod.subprocess, "Popen", _fake_popen)

    def _fake_create_connection(_addr: tuple[str, int], timeout: float) -> _FakeSocket:
        del timeout
        return _FakeSocket()

    monkeypatch.setattr(hashserv_mod.socket, "create_connection", _fake_create_connection)

    expected_port = hashserv_mod._workspace_port(tmp_path)
    url = hashserv_mod.ensure_running(tmp_path, binary_root=tmp_path)

    assert url == f"ws://localhost:{expected_port}"
    pid_file = tmp_path / ".bakar" / "hashserv.pid"
    port_file = tmp_path / ".bakar" / "hashserv.port"
    assert pid_file.read_text().strip() == "12345"
    assert port_file.read_text().strip() == str(expected_port)

    popen_args = captured_popen_args["args"]
    assert isinstance(popen_args, list)
    assert "--bind" in popen_args
    assert f"ws://localhost:{expected_port}" in popen_args
    assert "--database" in popen_args


def test_ensure_running_aborts_when_probe_times_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Probe never succeeds - no PID/port files, stderr captured, None returned."""
    import signal as signal_mod

    from bakar import hashserv as hashserv_mod

    _create_workspace_binary(tmp_path)
    monkeypatch.setattr(hashserv_mod, "is_running", lambda _root: False)

    fake_proc = _FakeProc(pid=12345, poll_returns=[None], stderr_bytes=b"timeout text")
    monkeypatch.setattr(
        hashserv_mod.subprocess,
        "Popen",
        lambda *_args, **_kwargs: fake_proc,
    )

    def _refuse(*_args: object, **_kwargs: object) -> None:
        raise ConnectionRefusedError

    monkeypatch.setattr(hashserv_mod.socket, "create_connection", _refuse)
    monkeypatch.setattr(hashserv_mod.time, "sleep", lambda _s: None)

    # Drive monotonic forward fast so the deadline trips after a few probes.
    fake_clock = {"now": 0.0}

    def _fake_monotonic() -> float:
        current = fake_clock["now"]
        fake_clock["now"] += 1.0
        return current

    monkeypatch.setattr(hashserv_mod.time, "monotonic", _fake_monotonic)

    sigterm_targets: list[int] = []

    def _fake_kill(pid: int, sig: int) -> None:
        if sig == signal_mod.SIGTERM:
            sigterm_targets.append(pid)

    monkeypatch.setattr(hashserv_mod.os, "kill", _fake_kill)

    result = hashserv_mod.ensure_running(tmp_path, binary_root=tmp_path)

    assert result is None
    assert not (tmp_path / ".bakar" / "hashserv.pid").exists()
    assert not (tmp_path / ".bakar" / "hashserv.port").exists()
    # stderr goes directly to the file at spawn time (not via PIPE drain).
    stderr_file = tmp_path / ".bakar" / "hashserv.stderr"
    assert stderr_file.exists()
    assert sigterm_targets == [12345]


def test_ensure_running_handles_immediate_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Daemon exits before any probe - capture stderr, return None, no PID file."""
    from bakar import hashserv as hashserv_mod

    _create_workspace_binary(tmp_path)
    monkeypatch.setattr(hashserv_mod, "is_running", lambda _root: False)

    fake_proc = _FakeProc(pid=12345, poll_returns=[1], stderr_bytes=b"db locked")
    monkeypatch.setattr(
        hashserv_mod.subprocess,
        "Popen",
        lambda *_args, **_kwargs: fake_proc,
    )

    def _should_not_probe(*_args: object, **_kwargs: object) -> None:
        msg = "socket probe must not run when proc.poll() reports immediate exit"
        raise AssertionError(msg)

    monkeypatch.setattr(hashserv_mod.socket, "create_connection", _should_not_probe)

    result = hashserv_mod.ensure_running(tmp_path, binary_root=tmp_path)

    assert result is None
    assert not (tmp_path / ".bakar" / "hashserv.pid").exists()
    assert not (tmp_path / ".bakar" / "hashserv.port").exists()
    # stderr now goes directly to the file at spawn time (not via PIPE), so the
    # file exists but is empty in tests (the mock process doesn't write to it).
    stderr_file = tmp_path / ".bakar" / "hashserv.stderr"
    assert stderr_file.exists()


def test_stop_no_pid_file_returns_false(tmp_path: Path) -> None:
    """No PID file under .bakar/ - stop is a no-op and returns False."""
    from bakar.hashserv import stop

    assert stop(tmp_path) is False


def test_stop_signals_alive_pid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live daemon dies on SIGTERM; PID + port files removed, DB stays."""
    import signal as signal_mod

    from bakar import hashserv as hashserv_mod

    state_dir = tmp_path / ".bakar"
    state_dir.mkdir(parents=True)
    pid_file = state_dir / "hashserv.pid"
    pid_file.write_text("12345\n")
    port_file = state_dir / "hashserv.port"
    port_file.write_text("50000\n")
    db_file = state_dir / "hashserv.db"
    db_file.write_bytes(b"sqlite-cache-content")

    signals_sent: list[tuple[int, int]] = []

    def _fake_kill(pid: int, sig: int) -> None:
        signals_sent.append((pid, sig))

    monkeypatch.setattr(hashserv_mod.os, "kill", _fake_kill)

    # Daemon "dies" the first time stop polls is_running after SIGTERM.
    is_running_calls = {"n": 0}

    def _fake_is_running(_root: Path) -> bool:
        is_running_calls["n"] += 1
        return False

    monkeypatch.setattr(hashserv_mod, "is_running", _fake_is_running)
    monkeypatch.setattr(hashserv_mod.time, "sleep", lambda _s: None)

    assert hashserv_mod.stop(tmp_path) is True
    assert (12345, signal_mod.SIGTERM) in signals_sent
    assert not any(sig == signal_mod.SIGKILL for _pid, sig in signals_sent)
    assert not pid_file.exists()
    assert not port_file.exists()
    assert db_file.exists()
    assert db_file.read_bytes() == b"sqlite-cache-content"


def test_stop_preserves_db_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SQLite database survives stop() so cache accumulates across cycles."""
    from bakar import hashserv as hashserv_mod

    state_dir = tmp_path / ".bakar"
    state_dir.mkdir(parents=True)
    (state_dir / "hashserv.pid").write_text("12345\n")
    (state_dir / "hashserv.port").write_text("50000\n")
    db_file = state_dir / "hashserv.db"
    db_payload = b"\x00\x01\x02SQLite cache bytes - must survive stop()"
    db_file.write_bytes(db_payload)

    monkeypatch.setattr(hashserv_mod.os, "kill", lambda _pid, _sig: None)
    monkeypatch.setattr(hashserv_mod, "is_running", lambda _root: False)
    monkeypatch.setattr(hashserv_mod.time, "sleep", lambda _s: None)

    result = hashserv_mod.stop(tmp_path)

    assert result is True
    assert db_file.exists()
    assert db_file.read_bytes() == db_payload


def test_stop_force_kills_after_grace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When SIGTERM grace lapses with the daemon still alive, SIGKILL fires."""
    import signal as signal_mod

    from bakar import hashserv as hashserv_mod

    state_dir = tmp_path / ".bakar"
    state_dir.mkdir(parents=True)
    (state_dir / "hashserv.pid").write_text("12345\n")
    (state_dir / "hashserv.port").write_text("50000\n")

    signals_sent: list[int] = []

    def _fake_kill(_pid: int, sig: int) -> None:
        signals_sent.append(sig)

    monkeypatch.setattr(hashserv_mod.os, "kill", _fake_kill)
    # Daemon never dies, so the grace polls all see True and SIGKILL fires.
    monkeypatch.setattr(hashserv_mod, "is_running", lambda _root: True)
    # Without this the test would block for _TERM_GRACE_SECONDS (5s) of real
    # sleep before the SIGKILL fallback triggers.
    monkeypatch.setattr(hashserv_mod.time, "sleep", lambda _s: None)

    # Drive monotonic forward fast so the grace deadline trips immediately.
    fake_clock = {"now": 0.0}

    def _fake_monotonic() -> float:
        current = fake_clock["now"]
        fake_clock["now"] += 1.0
        return current

    monkeypatch.setattr(hashserv_mod.time, "monotonic", _fake_monotonic)

    result = hashserv_mod.stop(tmp_path)

    assert result is True
    assert signal_mod.SIGTERM in signals_sent
    assert signal_mod.SIGKILL in signals_sent
    # SIGTERM must precede SIGKILL - the graceful-then-force ordering matters.
    assert signals_sent.index(signal_mod.SIGTERM) < signals_sent.index(signal_mod.SIGKILL)


def test_stop_handles_already_dead_pid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recorded PID is already dead - state files still cleaned, returns True."""
    from bakar import hashserv as hashserv_mod

    state_dir = tmp_path / ".bakar"
    state_dir.mkdir(parents=True)
    pid_file = state_dir / "hashserv.pid"
    pid_file.write_text("12345\n")
    port_file = state_dir / "hashserv.port"
    port_file.write_text("50000\n")
    db_file = state_dir / "hashserv.db"
    db_file.write_bytes(b"cache")

    def _kill_raises(_pid: int, _sig: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(hashserv_mod.os, "kill", _kill_raises)
    monkeypatch.setattr(hashserv_mod, "is_running", lambda _root: False)
    monkeypatch.setattr(hashserv_mod.time, "sleep", lambda _s: None)

    result = hashserv_mod.stop(tmp_path)

    assert result is True
    assert not pid_file.exists()
    assert not port_file.exists()
    assert db_file.exists()


def test_ensure_running_keys_state_to_state_key_not_binary_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The DB/port/PID live under state_key while the binary is found under
    binary_root - the two can be different directories (sstate vs workspace)."""
    from bakar import hashserv as hashserv_mod

    binary_root = tmp_path / "workspace"
    state_key = tmp_path / "sstate"
    binary_root.mkdir()
    state_key.mkdir()
    _create_workspace_binary(binary_root)

    monkeypatch.setattr(hashserv_mod, "is_running", lambda _root: False)

    fake_proc = _FakeProc(pid=4242, poll_returns=[None])
    captured: dict[str, object] = {}

    def _fake_popen(args: list[str], **kwargs: object) -> _FakeProc:
        captured["args"] = args
        return fake_proc

    monkeypatch.setattr(hashserv_mod.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(
        hashserv_mod.socket,
        "create_connection",
        lambda _addr, timeout: _FakeSocket(),
    )

    expected_port = hashserv_mod._workspace_port(state_key)
    url = hashserv_mod.ensure_running(state_key, binary_root=binary_root)

    assert url == f"ws://localhost:{expected_port}"
    # State files land under state_key, not binary_root.
    assert (state_key / ".bakar" / "hashserv.pid").read_text().strip() == "4242"
    assert (state_key / ".bakar" / "hashserv.port").read_text().strip() == str(expected_port)
    assert not (binary_root / ".bakar" / "hashserv.pid").exists()
    # The spawned binary is the one under binary_root, and the DB is under state_key.
    popen_args = captured["args"]
    assert isinstance(popen_args, list)
    assert str(binary_root / "sources" / "poky" / "bitbake" / "bin" / "bitbake-hashserv") in popen_args
    assert str(state_key / ".bakar" / "hashserv.db") in popen_args


def test_ensure_running_returns_none_when_binary_root_missing_binary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No binary under binary_root - return None even if state_key exists."""
    from bakar import hashserv as hashserv_mod

    state_key = tmp_path / "sstate"
    state_key.mkdir()
    monkeypatch.setattr(hashserv_mod, "is_running", lambda _root: False)

    assert hashserv_mod.ensure_running(state_key, binary_root=tmp_path / "workspace") is None


def test_binary_available_checks_binary_root(tmp_path: Path) -> None:
    """binary_available probes the binary_root, independent of any state key."""
    from bakar.hashserv import binary_available

    binary_root = tmp_path / "workspace"
    binary_root.mkdir()
    assert binary_available(binary_root) is False

    _create_workspace_binary(binary_root)
    assert binary_available(binary_root) is True
