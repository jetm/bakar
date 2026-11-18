"""Tests for bakar.daemon_log.record_refusal."""

from __future__ import annotations

from typing import TYPE_CHECKING

from bakar import daemon_log

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_writes_exact_bytes(tmp_path: Path) -> None:
    log = tmp_path / "x.stderr"
    daemon_log.record_refusal(log, tmp_path, "port busy")
    assert log.read_bytes() == b"bakar: port busy\n"


def test_replaces_earlier_content(tmp_path: Path) -> None:
    log = tmp_path / "x.stderr"
    log.write_bytes(b"old line one\nold line two\n")
    daemon_log.record_refusal(log, tmp_path, "new")
    assert log.read_bytes() == b"bakar: new\n"


def test_append_keeps_earlier_content_and_adds_the_line(tmp_path: Path) -> None:
    log = tmp_path / "x.stderr"
    log.write_bytes(b"daemon output\n")
    daemon_log.record_refusal(log, tmp_path, "new", append=True)
    assert log.read_bytes() == b"daemon output\nbakar: new\n"


def test_append_creates_a_missing_log(tmp_path: Path) -> None:
    log = tmp_path / "state" / "x.stderr"
    daemon_log.record_refusal(log, log.parent, "new", append=True)
    assert log.read_bytes() == b"bakar: new\n"


def test_unwritable_state_dir_does_not_raise(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")
    state_dir = blocker / "state"
    daemon_log.record_refusal(state_dir / "x.stderr", state_dir, "text")
    assert blocker.read_text() == "x"


class _FailingFh:
    def __init__(self, *, fail_write: bool, fail_close: bool) -> None:
        self.fail_write = fail_write
        self.fail_close = fail_close

    def write(self, data: bytes) -> int:
        if self.fail_write:
            raise OSError(28, "No space left on device")
        return len(data)

    def close(self) -> None:
        if self.fail_close:
            raise OSError(28, "No space left on device")


def _patch_open(monkeypatch: pytest.MonkeyPatch, fh: _FailingFh) -> None:
    from contextlib import contextmanager

    @contextmanager
    def fake(binary: str, state_dir: Path, log_path: Path | None = None):
        try:
            yield fh
        finally:
            fh.close()

    monkeypatch.setattr(daemon_log, "stderr_target", fake)


def test_write_oserror_swallowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_open(monkeypatch, _FailingFh(fail_write=True, fail_close=False))
    daemon_log.record_refusal(tmp_path / "x", tmp_path, "t")


def test_close_oserror_swallowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_open(monkeypatch, _FailingFh(fail_write=False, fail_close=True))
    daemon_log.record_refusal(tmp_path / "x", tmp_path, "t")
