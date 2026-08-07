"""Structured milestone records for the systemd journal.

Full build output stays in ``kas.log``; the journal gets a small, fixed set of
structured records - start, phase changes, task failures, a periodic progress
snapshot, end - so a run's shape is queryable with ``journalctl`` right next to
the lines systemd already writes for that build's scope (``Consumed ... CPU
time``, memory peaks, OOM kills). Correlating build progress against those
resource events is the thing a log file cannot do.

The firehose deliberately stays out. journald rate-limits to 10,000 messages
per 30s by default and drops the excess *silently*, and a scope unit cannot
raise that limit (``LogRateLimitIntervalSec`` is an exec-context property a
scope has no way to set - see :mod:`bakar.build_scope`). A build emitting tens
of thousands of lines into the journal would therefore lose an unknowable
subset of them, which is the worst possible property for a log read only after
something broke.

Records are sent with journald's native wire protocol over a datagram socket,
so this needs no ``systemd`` Python bindings and degrades to a no-op wherever
the socket is absent (a container, a non-systemd host, a stripped CI image).
"""

from __future__ import annotations

import os
import shutil
import socket
from pathlib import Path
from typing import TYPE_CHECKING

from bakar import psi

if TYPE_CHECKING:
    from collections.abc import Mapping

JOURNAL_SOCKET = Path("/run/systemd/journal/socket")

# syslog levels, the subset this module emits.
PRIORITY_ERROR = 3
PRIORITY_WARNING = 4
PRIORITY_INFO = 6

_IDENTIFIER = "bakar"

# Field prefix for everything bakar adds, so `journalctl BAKAR_EVENT=progress`
# filters cleanly and nothing collides with journald's own trusted fields.
_PREFIX = "BAKAR_"


def _encode_field(key: str, value: str) -> bytes:
    """Encode one field in journald's native wire format.

    A value containing no newline is sent as ``KEY=value``; anything else needs
    the binary framing (bare ``KEY``, then a 64-bit little-endian length, then
    the raw bytes) because the plain form is newline-delimited and would
    otherwise be parsed as several truncated fields.
    """
    raw = value.encode("utf-8", "replace")
    if b"\n" in raw:
        return key.encode("ascii") + b"\n" + len(raw).to_bytes(8, "little") + raw + b"\n"
    return key.encode("ascii") + b"=" + raw + b"\n"


def encode_record(fields: Mapping[str, str]) -> bytes:
    """Serialise a whole record. Exposed separately so tests can assert framing."""
    return b"".join(_encode_field(key, value) for key, value in fields.items())


class JournalEmitter:
    """Best-effort sender of structured build records to the local journal.

    Never raises and never blocks a build: a missing socket disables the
    emitter at construction, and a send failure is swallowed and reported by
    the return value. Callers treat emission as telemetry, not as a step.
    """

    def __init__(
        self,
        base_fields: Mapping[str, str] | None = None,
        *,
        enabled: bool = True,
        socket_path: Path = JOURNAL_SOCKET,
    ) -> None:
        self._base = {f"{_PREFIX}{k.upper()}": str(v) for k, v in (base_fields or {}).items()}
        self._socket_path = socket_path
        self._sock: socket.socket | None = None
        # A datagram socket to an absent path fails on every send, so decide
        # once here rather than paying an exception per record.
        self.enabled = enabled and socket_path.exists()

    def send(self, event: str, message: str, *, priority: int = PRIORITY_INFO, **fields: object) -> bool:
        """Emit one record. Returns True when it reached the journal.

        ``fields`` are upper-cased and prefixed; a None value is dropped rather
        than serialised as the string "None", so an unavailable probe leaves the
        field absent instead of poisoning it with a placeholder.
        """
        if not self.enabled:
            return False
        record = {
            "MESSAGE": message,
            "PRIORITY": str(priority),
            "SYSLOG_IDENTIFIER": _IDENTIFIER,
            f"{_PREFIX}EVENT": event,
            **self._base,
        }
        for key, value in fields.items():
            if value is None:
                continue
            record[f"{_PREFIX}{key.upper()}"] = str(value)
        try:
            if self._sock is None:
                self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            self._sock.sendto(encode_record(record), str(self._socket_path))
        except OSError:
            return False
        return True

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None


def _meminfo() -> dict[str, int]:
    """Parse the few /proc/meminfo keys worth carrying, in kB."""
    wanted = {"MemAvailable", "SwapFree", "SwapTotal"}
    values: dict[str, int] = {}
    try:
        raw = Path("/proc/meminfo").read_text()
    except OSError:
        return values
    for line in raw.splitlines():
        key, _, rest = line.partition(":")
        if key in wanted:
            try:
                values[key] = int(rest.split()[0])
            except IndexError, ValueError:
                continue
    return values


def health_fields(tmpdir: Path | None = None) -> dict[str, str]:
    """OS counters that explain a build that is slow rather than broken.

    Load and PSI separate "saturated" from "idle and stuck" - the distinction
    that made an NFS-delegation stall read as a hang. Swap usage catches a host
    thrashing into zram, and free space on the build tmpdir catches the failure
    that otherwise surfaces as an unrelated task error much later.
    """
    fields: dict[str, str] = {}
    try:
        fields["LOAD1"] = f"{os.getloadavg()[0]:.2f}"
    except OSError:
        pass
    for resource in ("cpu", "io", "memory"):
        value = psi.read_psi_avg10(resource)
        if value is not None:
            fields[f"PSI_{resource.upper()}"] = f"{value:.2f}"
    mem = _meminfo()
    if "MemAvailable" in mem:
        fields["MEM_AVAIL_MB"] = str(mem["MemAvailable"] // 1024)
    if "SwapTotal" in mem and "SwapFree" in mem:
        fields["SWAP_USED_MB"] = str((mem["SwapTotal"] - mem["SwapFree"]) // 1024)
    if tmpdir is not None:
        try:
            fields["DISK_FREE_GB"] = f"{shutil.disk_usage(tmpdir).free / 1024**3:.1f}"
        except OSError:
            pass
    return fields


def cache_fields(daemon_doc: Mapping | None, ccache_doc: Mapping | None) -> dict[str, str]:
    """sccache/ccache counters, so a cache that silently stopped hitting is visible.

    A build that loses its cache does not fail - it just gets slower, which is
    invisible without the hit rate beside the progress record.
    """
    fields: dict[str, str] = {}
    if daemon_doc is not None:
        hits = daemon_doc.get("cache_hits")
        misses = daemon_doc.get("cache_misses")
        if hits is not None and misses is not None:
            total = hits + misses
            fields["SCCACHE_HITS"] = str(hits)
            fields["SCCACHE_MISSES"] = str(misses)
            if total:
                fields["SCCACHE_HIT_PCT"] = f"{hits / total * 100:.1f}"
        verdict = daemon_doc.get("verdict")
        if verdict is not None:
            fields["SCCACHE_VERDICT"] = str(verdict)
    if ccache_doc is not None:
        hit_rate = ccache_doc.get("hit_rate")
        if hit_rate is not None:
            fields["CCACHE_HIT_PCT"] = f"{float(hit_rate):.1f}"
    return fields
