"""``stream_bash_script`` and ``stream_on_host``: the streaming ``bash -s`` transport."""

from __future__ import annotations

import os
import shlex
import subprocess
from typing import TYPE_CHECKING, Any, ClassVar

import pytest

from bakar.commands import status as status_mod
from bakar.steps import remote_dispatch as rd

if TYPE_CHECKING:
    from collections.abc import Iterator


class _Stdin:
    def __init__(self, *, broken: bool = False) -> None:
        self.broken = broken
        self.written = ""
        self.was_closed = False

    def write(self, data: bytes) -> int:
        if self.broken:
            raise BrokenPipeError
        self.written += data.decode("utf-8")
        return len(data)

    def close(self) -> None:
        self.was_closed = True


class _Stdout:
    """The read end of a real pipe, as ``Popen.stdout`` exposes it."""

    def __init__(self, fd: int) -> None:
        self.fd = fd

    def fileno(self) -> int:
        return self.fd


class FakePopen:
    instances: ClassVar[list[FakePopen]] = []
    lines: ClassVar[list[str]] = []
    returncode = 0
    broken = False
    hang_on_terminate = False
    quiet = False
    silenced: ClassVar[list[bool]] = []

    def __init__(self, argv: list[str], **kwargs: Any) -> None:
        self.argv = argv
        self.kwargs = kwargs
        self.stdin = _Stdin(broken=type(self).broken)
        read_fd, self._write_fd = os.pipe()
        self.stdout = _Stdout(read_fd)
        os.write(self._write_fd, "".join(type(self).lines).encode())
        if not type(self).quiet:
            self._close_write()
        self.terminated = False
        self.killed = False
        self.waited = False
        self.wait_timeouts: list[float | None] = []
        type(self).instances.append(self)

    def _close_write(self) -> None:
        if self._write_fd >= 0:
            os.close(self._write_fd)
            self._write_fd = -1

    def poll(self) -> int | None:
        return type(self).returncode if (self.waited or self.killed) else None

    def wait(self, timeout: float | None = None) -> int:
        self.wait_timeouts.append(timeout)
        if timeout is not None and type(self).hang_on_terminate and not self.killed:
            raise subprocess.TimeoutExpired(self.argv, timeout)
        self.waited = True
        return type(self).returncode

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def release(self) -> None:
        self._close_write()
        os.close(self.stdout.fd)


@pytest.fixture
def popen(monkeypatch: pytest.MonkeyPatch) -> Iterator[type[FakePopen]]:
    cls = type("P", (FakePopen,), {"instances": [], "lines": []})
    monkeypatch.setattr(rd.subprocess, "Popen", cls)
    monkeypatch.setattr(rd, "_STREAM_POLL_SECONDS", 0.01)
    monkeypatch.setattr(rd, "stdout_reader_gone", lambda fd=1: False)
    silenced: list[bool] = []
    monkeypatch.setattr(rd, "_silence_stdout", lambda: silenced.append(True))
    cls.silenced = silenced
    yield cls
    for proc in cls.instances:
        proc.release()


@pytest.fixture
def no_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(status_mod, "_preflight", lambda host, *, transport_exit: None)


def test_argv_is_bash_s_over_batch_ssh(popen: type[FakePopen], no_preflight: None) -> None:
    status_mod.stream_on_host("pc3", "log", ["--which", "kas"], transport_exit=2)
    (proc,) = popen.instances
    assert proc.argv == ["ssh", *rd.SSH_BOUND_OPTS, "-o", "BatchMode=yes", "pc3", "bash", "-s"]
    assert proc.kwargs["stdin"] is subprocess.PIPE
    assert proc.kwargs["stdout"] is subprocess.PIPE
    assert proc.kwargs["stderr"] is None
    assert "shell" not in proc.kwargs


def test_script_goes_over_stdin_and_is_closed(popen: type[FakePopen], no_preflight: None) -> None:
    status_mod.stream_on_host("pc3", "log", ["--which", "kas", "--run", "20261020-101010"], transport_exit=2)
    (proc,) = popen.instances
    assert proc.stdin.written == "bakar log --which kas --run 20261020-101010\n"
    assert proc.stdin.was_closed
    assert "bakar" not in proc.argv


@pytest.mark.parametrize("run_id", ["my run", "-x", "--json", "$(touch /tmp/pwned)"])
def test_awkward_run_id_stays_one_word(popen: type[FakePopen], no_preflight: None, run_id: str) -> None:
    status_mod.stream_on_host("pc3", "log", ["--which", "kas", "--run", run_id], transport_exit=2)
    script = popen.instances[0].stdin.written
    assert shlex.split(script) == ["bakar", "log", "--which", "kas", "--run", run_id]


def test_stdout_lines_stream_in_order(
    popen: type[FakePopen], no_preflight: None, capsys: pytest.CaptureFixture[str]
) -> None:
    popen.lines = ["one\n", "two\n", "three\n"]
    code = status_mod.stream_on_host("pc3", "log", [], transport_exit=2)
    out, err = capsys.readouterr()
    assert code == 0
    assert out == "one\ntwo\nthree\n"
    assert err == ""


def test_child_exit_status_is_returned(popen: type[FakePopen], no_preflight: None) -> None:
    popen.returncode = 1
    assert status_mod.stream_on_host("pc3", "log", [], transport_exit=2) == 1
    assert rd.stream_bash_script("pc3", "true\n") == 1


def test_transport_failure_returns_transport_exit(
    popen: type[FakePopen], no_preflight: None, capsys: pytest.CaptureFixture[str]
) -> None:
    popen.returncode = 255
    assert status_mod.stream_on_host("pc3", "log", [], transport_exit=2) == 2
    assert "bakar log: ssh to pc3 failed (exit 255)" in capsys.readouterr().err


def test_broken_pipe_returns_255_without_reading(popen: type[FakePopen], capsys: pytest.CaptureFixture[str]) -> None:
    popen.broken = True
    popen.lines = ["never\n"]
    assert rd.stream_bash_script("pc3", "bakar log\n") == 255
    out, err = capsys.readouterr()
    assert out == ""
    assert "connection to pc3 lost before the script was delivered" in err


def _interrupting_select(*_a: Any, **_k: Any) -> Any:
    raise KeyboardInterrupt


def test_ctrl_c_terminates_child_and_reraises(popen: type[FakePopen], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rd.select, "select", _interrupting_select)
    with pytest.raises(KeyboardInterrupt):
        rd.stream_bash_script("pc3", "bakar log\n")
    (proc,) = popen.instances
    assert proc.terminated
    assert not proc.killed
    assert 5 in proc.wait_timeouts


def test_ctrl_c_kills_child_that_ignores_terminate(popen: type[FakePopen], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rd.select, "select", _interrupting_select)
    popen.hang_on_terminate = True
    with pytest.raises(KeyboardInterrupt):
        rd.stream_bash_script("pc3", "bakar log\n")
    (proc,) = popen.instances
    assert proc.terminated
    assert proc.killed


class _ClosedBuffer:
    def write(self, _data: bytes) -> int:
        raise BrokenPipeError

    def flush(self) -> None:
        raise BrokenPipeError


class _ClosedStdout:
    buffer = _ClosedBuffer()


def test_closed_local_stdout_returns_zero_and_terminates_child(
    popen: type[FakePopen], monkeypatch: pytest.MonkeyPatch
) -> None:
    popen.lines = ["one\n"]
    popen.quiet = True
    popen.returncode = 7
    monkeypatch.setattr(rd.sys, "stdout", _ClosedStdout())
    assert rd.stream_bash_script("pc3", "bakar log\n") == 0
    (proc,) = popen.instances
    assert proc.terminated
    assert popen.silenced == [True]


def test_reader_gone_on_quiet_remote_returns_zero_and_terminates_child(
    popen: type[FakePopen], monkeypatch: pytest.MonkeyPatch
) -> None:
    popen.quiet = True
    popen.returncode = 7
    monkeypatch.setattr(rd, "stdout_reader_gone", lambda fd=1: True)
    assert rd.stream_bash_script("pc3", "bakar log\n") == 0
    (proc,) = popen.instances
    assert proc.terminated
    assert popen.silenced == [True]


def test_stream_that_ends_normally_does_not_terminate_child(popen: type[FakePopen]) -> None:
    popen.lines = ["x\n"]
    assert rd.stream_bash_script("pc3", "true\n") == 0
    (proc,) = popen.instances
    assert not proc.terminated
    assert popen.silenced == []


def test_reader_gone_kills_child_that_ignores_terminate(
    popen: type[FakePopen], monkeypatch: pytest.MonkeyPatch
) -> None:
    popen.quiet = True
    popen.hang_on_terminate = True
    monkeypatch.setattr(rd, "stdout_reader_gone", lambda fd=1: True)
    assert rd.stream_bash_script("pc3", "bakar log\n") == 0
    (proc,) = popen.instances
    assert proc.terminated
    assert proc.killed


def test_ctrl_c_propagates_through_stream_on_host(
    popen: type[FakePopen], no_preflight: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rd.select, "select", _interrupting_select)
    with pytest.raises(KeyboardInterrupt):
        status_mod.stream_on_host("pc3", "log", [], transport_exit=2)
    assert popen.instances[0].terminated


def test_preflight_refusal_never_starts_ssh(popen: type[FakePopen], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(status_mod, "_preflight", lambda host, *, transport_exit: 2)
    assert status_mod.stream_on_host("pc3", "log", [], transport_exit=3) == 2
    assert popen.instances == []


def test_dash_host_is_refused_before_ssh(popen: type[FakePopen], monkeypatch: pytest.MonkeyPatch) -> None:
    capture = []
    monkeypatch.setattr(rd, "_ssh_bash_capture", lambda *a, **k: capture.append(a))
    assert status_mod.stream_on_host("-oProxyCommand=x", "log", [], transport_exit=2) == 2
    assert capture == []
    assert popen.instances == []
