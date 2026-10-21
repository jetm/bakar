"""Tests for the log follower's gone-reader exit and its bounded end-of-file read."""

from __future__ import annotations

import os
import select
import signal
import subprocess
import sys
import textwrap
from typing import TYPE_CHECKING

import pytest

from bakar.commands.log import _last_lines
from bakar.output_mode import silence_stdout, stdout_reader_gone

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# (a) stdout_reader_gone
# ---------------------------------------------------------------------------


def test_reader_gone_true_for_pipe_with_closed_read_end() -> None:
    r, w = os.pipe()
    try:
        assert stdout_reader_gone(w) is False
        os.close(r)
        r = -1
        assert stdout_reader_gone(w) is True
    finally:
        if r != -1:
            os.close(r)
        os.close(w)


def test_reader_gone_false_for_regular_file(tmp_path: Path) -> None:
    with (tmp_path / "out.txt").open("wb") as fh:
        assert stdout_reader_gone(fh.fileno()) is False


def test_reader_gone_false_for_dev_null() -> None:
    fd = os.open(os.devnull, os.O_WRONLY)
    try:
        assert stdout_reader_gone(fd) is False
    finally:
        os.close(fd)


def test_reader_gone_false_without_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    r, w = os.pipe()
    os.close(r)
    try:
        monkeypatch.delattr(select, "poll")
        assert stdout_reader_gone(w) is False
    finally:
        os.close(w)


# ---------------------------------------------------------------------------
# (b), (c) _last_lines
# ---------------------------------------------------------------------------


def _write_numbered(path: Path, count: int, width: int = 60) -> list[bytes]:
    lines = [f"line {i:06d} ".ljust(width, "x").encode() + b"\n" for i in range(count)]
    path.write_bytes(b"".join(lines))
    return lines


def test_last_lines_grows_window_past_first_block(tmp_path: Path) -> None:
    log = tmp_path / "kas.log"
    lines = _write_numbered(log, 5000)
    assert sum(len(x) for x in lines[-2000:]) > 65536
    with log.open("rb") as fh:
        got, truncated = _last_lines(fh, 2000)
        assert fh.tell() == log.stat().st_size
    assert got == lines[-2000:]
    assert got[0].startswith(b"line ")
    assert truncated is False


def test_last_lines_whole_small_file(tmp_path: Path) -> None:
    log = tmp_path / "kas.log"
    lines = _write_numbered(log, 10)
    with log.open("rb") as fh:
        got, truncated = _last_lines(fh, 40)
    assert got == lines
    assert truncated is False


def test_last_lines_stops_at_max_bytes(tmp_path: Path) -> None:
    log = tmp_path / "kas.log"
    lines = _write_numbered(log, 5000)
    with log.open("rb") as fh:
        got, truncated = _last_lines(fh, 2000, max_bytes=4096)
    assert 0 < len(got) < 2000
    assert got == lines[-len(got) :]
    assert got[0].startswith(b"line ")
    assert truncated is True


# ---------------------------------------------------------------------------
# (d) _tail_follow exits when its stdout reader is gone
# ---------------------------------------------------------------------------

_CHILD = textwrap.dedent(
    """
    import sys
    from pathlib import Path
    from bakar.commands.log import _tail_follow
    _tail_follow(Path(sys.argv[1]))
    """
)


def _run_follower(log: Path, err: Path) -> subprocess.Popen[bytes]:
    r, w = os.pipe()
    with err.open("wb") as err_fh:
        child = subprocess.Popen(
            [sys.executable, "-c", _CHILD, str(log)],
            stdin=subprocess.DEVNULL,
            stdout=w,
            stderr=err_fh,
            start_new_session=True,
        )
    os.close(w)
    os.close(r)
    return child


def _wait_or_kill(child: subprocess.Popen[bytes], timeout: float) -> int | None:
    try:
        return child.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        # Signal only the child's own session, and only when it really leads one.
        if os.getpgid(child.pid) == child.pid:
            os.killpg(child.pid, signal.SIGKILL)
        child.wait()
        return None


@pytest.mark.parametrize("content", [b"", b"history line\n" * 50], ids=["empty", "with-history"])
def test_tail_follow_returns_when_reader_closes(tmp_path: Path, content: bytes) -> None:
    log = tmp_path / "kas.log"
    log.write_bytes(content)
    err = tmp_path / "stderr.txt"
    child = _run_follower(log, err)
    code = _wait_or_kill(child, timeout=5)
    assert code == 0, f"follower still running or failed; stderr: {err.read_text()!r}"
    assert "Exception ignored" not in err.read_text()


# ---------------------------------------------------------------------------
# (d) silence_stdout is best effort
# ---------------------------------------------------------------------------


def test_silence_stdout_swallows_open_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: object, **_k: object) -> int:
        raise OSError("no fds left")

    dup2_calls: list[tuple[int, int]] = []
    monkeypatch.setattr("bakar.output_mode.os.open", boom)
    monkeypatch.setattr("bakar.output_mode.os.dup2", lambda a, b: dup2_calls.append((a, b)))

    silence_stdout()

    assert dup2_calls == []


def test_silence_stdout_closes_devnull_when_dup2_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    opened = os.open(os.devnull, os.O_WRONLY)
    closed: list[int] = []

    def fake_dup2(_src: int, _dst: int) -> None:
        raise OSError("dup2 refused")

    try:
        with monkeypatch.context() as mp:
            mp.setattr("bakar.output_mode.os.open", lambda *_a, **_k: opened)
            mp.setattr("bakar.output_mode.os.dup2", fake_dup2)
            mp.setattr("bakar.output_mode.os.close", closed.append)
            silence_stdout()
    finally:
        os.close(opened)

    assert closed == [opened]
