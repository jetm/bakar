"""Unit tests for the shared central hashserv/prserv daemon spawn logic.

The fix that matters: ``ensure_running`` redirects a spawned daemon's stderr
to a state-dir log file instead of ``subprocess.DEVNULL``, so a daemon that
starts but crashes or never listens leaves its diagnostic output on disk.
These tests monkeypatch ``central_service._STATE_DIR`` to ``tmp_path`` so no
test run touches the real ``~/.local/state/bakar`` directory.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from bakar import central_service

if TYPE_CHECKING:
    from pathlib import Path


class _FakeProc:
    """Stand-in for ``subprocess.Popen`` result that never exits on its own."""

    def poll(self) -> int | None:
        return None


@pytest.mark.unit
def test_ensure_running_captures_daemon_stderr_to_state_dir_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A spawned daemon's stderr goes to a file under the state dir, not DEVNULL.

    Before this fix ``ensure_running`` passed ``stderr=DEVNULL``, discarding
    whatever diagnostic output would have explained why a daemon started but
    never began listening.
    """
    monkeypatch.setattr(central_service, "_STATE_DIR", tmp_path)
    responses = iter([False, True])  # pre-spawn probe, then post-spawn probe
    monkeypatch.setattr(central_service, "is_listening", lambda host, port, **_: next(responses))
    monkeypatch.setattr(central_service.shutil, "which", lambda name: name)

    captured: dict = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        kwargs["stderr"].write(b"daemon startup error\n")
        return _FakeProc()

    monkeypatch.setattr(central_service.subprocess, "Popen", fake_popen)

    endpoint = central_service.ensure_running(
        binary="/usr/bin/bb-hashserv",
        bind_host="127.0.0.1",
        database=str(tmp_path / "hashserv.db"),
        port=9000,
    )

    assert endpoint == "127.0.0.1:9000"
    assert captured["kwargs"]["stdout"] == central_service.subprocess.DEVNULL
    assert captured["kwargs"]["start_new_session"] is True

    log_path = tmp_path / "bb-hashserv-central.stderr"
    assert log_path.exists()
    assert log_path.read_bytes() == b"daemon startup error\n"


@pytest.mark.unit
def test_ensure_running_short_circuits_without_writing_stderr_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An already-listening service never spawns, so no stderr log is created."""
    monkeypatch.setattr(central_service, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(central_service, "is_listening", lambda host, port, **_: True)
    spawned: list[object] = []
    monkeypatch.setattr(central_service.subprocess, "Popen", lambda *a, **k: spawned.append((a, k)))

    endpoint = central_service.ensure_running(
        binary="/usr/bin/bb-hashserv",
        bind_host="127.0.0.1",
        database=str(tmp_path / "hashserv.db"),
        port=9000,
    )

    assert endpoint == "127.0.0.1:9000"
    assert spawned == []
    assert not (tmp_path / "bb-hashserv-central.stderr").exists()


@pytest.mark.unit
def test_ensure_running_returns_none_when_binary_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No resolvable binary -> None, and never attempts a spawn or log write."""
    monkeypatch.setattr(central_service, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(central_service, "is_listening", lambda host, port, **_: False)
    monkeypatch.setattr(central_service.shutil, "which", lambda name: None)
    spawned: list[object] = []
    monkeypatch.setattr(central_service.subprocess, "Popen", lambda *a, **k: spawned.append((a, k)))

    endpoint = central_service.ensure_running(
        binary="nonexistent-hashserv-binary",
        bind_host="127.0.0.1",
        database=str(tmp_path / "hashserv.db"),
        port=9000,
    )

    assert endpoint is None
    assert spawned == []
