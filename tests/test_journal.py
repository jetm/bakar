"""Tests for structured journal milestone records.

The wire-format tests matter more than they look: journald parses the native
protocol positionally, so a value carrying an embedded newline that is not
binary-framed is silently reinterpreted as several truncated fields rather than
rejected. A record that looks fine in Python can therefore land corrupt.
"""

from __future__ import annotations

import socket
import threading
from typing import TYPE_CHECKING

import pytest

from bakar import journal

if TYPE_CHECKING:
    from pathlib import Path


def _read_one(sock: socket.socket) -> bytes:
    sock.settimeout(2)
    return sock.recv(65536)


@pytest.fixture
def journal_socket(tmp_path: Path):
    """A stand-in journald datagram socket that captures one record."""
    path = tmp_path / "socket"
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.bind(str(path))
    yield path, sock
    sock.close()


# --- wire format -----------------------------------------------------------


def test_plain_field_uses_key_equals_value() -> None:
    assert journal.encode_record({"MESSAGE": "hello"}) == b"MESSAGE=hello\n"


def test_multiline_value_uses_binary_framing() -> None:
    """A newline in a value must switch to the length-prefixed form."""
    out = journal.encode_record({"MESSAGE": "a\nb"})
    assert out == b"MESSAGE\n" + (3).to_bytes(8, "little") + b"a\nb\n"
    # The naive form would have produced a bare "b" line that journald reads as
    # a separate (invalid) field.
    assert out != b"MESSAGE=a\nb\n"


def test_non_ascii_value_survives_encoding() -> None:
    out = journal.encode_record({"MESSAGE": "café"})
    assert "café".encode() in out


# --- emitter ---------------------------------------------------------------


def test_send_delivers_prefixed_structured_fields(journal_socket) -> None:
    path, sock = journal_socket
    emitter = journal.JournalEmitter({"run_id": "20260807-101500"}, socket_path=path)
    assert emitter.enabled
    assert emitter.send("progress", "build progress", tasks_done=42, pct="17.5")
    payload = _read_one(sock).decode()
    assert "BAKAR_EVENT=progress" in payload
    assert "BAKAR_RUN_ID=20260807-101500" in payload
    assert "BAKAR_TASKS_DONE=42" in payload
    assert "BAKAR_PCT=17.5" in payload
    assert "SYSLOG_IDENTIFIER=bakar" in payload
    emitter.close()


def test_none_valued_field_is_omitted_not_stringified(journal_socket) -> None:
    """An unavailable probe must leave the field absent, not set it to "None"."""
    path, sock = journal_socket
    emitter = journal.JournalEmitter(socket_path=path)
    emitter.send("progress", "m", sstate_pct=None, tasks_done=1)
    payload = _read_one(sock).decode()
    assert "BAKAR_SSTATE_PCT" not in payload
    assert "BAKAR_TASKS_DONE=1" in payload
    emitter.close()


def test_disabled_by_config_sends_nothing(journal_socket) -> None:
    path, sock = journal_socket
    emitter = journal.JournalEmitter(socket_path=path, enabled=False)
    assert emitter.enabled is False
    assert emitter.send("progress", "m") is False
    sock.settimeout(0.2)
    with pytest.raises((TimeoutError, BlockingIOError, OSError)):
        sock.recv(1024)


def test_absent_socket_disables_emitter(tmp_path: Path) -> None:
    """No journald (container, non-systemd host) must degrade to a silent no-op."""
    emitter = journal.JournalEmitter(socket_path=tmp_path / "nope")
    assert emitter.enabled is False
    assert emitter.send("build_start", "m") is False


def test_send_failure_is_swallowed(tmp_path: Path) -> None:
    """A socket that exists but rejects delivery must not raise into the build."""
    path = tmp_path / "socket"
    path.write_text("not a socket")
    emitter = journal.JournalEmitter(socket_path=path)
    assert emitter.enabled  # exists() passed
    assert emitter.send("build_start", "m") is False


def test_emitter_is_safe_from_the_progress_thread(journal_socket) -> None:
    """The progress snapshot runs on a daemon thread; sending must not raise there."""
    path, sock = journal_socket
    emitter = journal.JournalEmitter(socket_path=path)
    errors: list[BaseException] = []

    def _worker() -> None:
        try:
            emitter.send("progress", "from thread")
        except BaseException as exc:  # noqa: BLE001 - the point is that none escapes
            errors.append(exc)

    thread = threading.Thread(target=_worker)
    thread.start()
    thread.join(timeout=2)
    assert errors == []
    assert b"from thread" in _read_one(sock)
    emitter.close()


# --- troubleshooting field collectors --------------------------------------


def test_health_fields_reports_load_and_memory() -> None:
    fields = journal.health_fields()
    assert "LOAD1" in fields
    assert float(fields["LOAD1"]) >= 0
    # MemAvailable is present on every supported kernel; guard rather than assume.
    if "MEM_AVAIL_MB" in fields:
        assert int(fields["MEM_AVAIL_MB"]) > 0


def test_health_fields_tolerates_missing_tmpdir(tmp_path: Path) -> None:
    """A tmpdir that does not exist yet must not raise from the sampler."""
    fields = journal.health_fields(tmp_path / "not-created")
    assert "DISK_FREE_GB" not in fields


def test_health_fields_reports_disk_free_for_real_path(tmp_path: Path) -> None:
    assert float(journal.health_fields(tmp_path)["DISK_FREE_GB"]) > 0


def test_cache_fields_computes_sccache_hit_rate() -> None:
    fields = journal.cache_fields({"cache_hits": 3, "cache_misses": 1, "verdict": "dist"}, None)
    assert fields["SCCACHE_HIT_PCT"] == "75.0"
    assert fields["SCCACHE_VERDICT"] == "dist"


def test_cache_fields_survives_a_cold_cache() -> None:
    """Zero hits and zero misses must not divide by zero."""
    fields = journal.cache_fields({"cache_hits": 0, "cache_misses": 0}, None)
    assert "SCCACHE_HIT_PCT" not in fields
    assert fields["SCCACHE_HITS"] == "0"


def test_cache_fields_reads_ccache_hit_rate() -> None:
    assert journal.cache_fields(None, {"hit_rate": 62.5})["CCACHE_HIT_PCT"] == "62.5"


def test_cache_fields_empty_without_any_cache() -> None:
    assert journal.cache_fields(None, None) == {}
