"""Per-unit records for detached builds, kept under the user runtime directory.

A detached build runs in a transient systemd unit that outlives the launching
shell. Three files per unit tie the unit back to the runs it produced:

* ``<unit>.log``      - captured output of the unit
* ``<unit>.log.rc``   - sentinel holding the exit status; its presence means
                         the unit finished
* ``<unit>.runs``     - JSON lines, one per run registered by the unit

Stdlib only and no bakar imports: this is a leaf consumed by the launcher, the
run logger and the status commands.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

UNIT_RE = re.compile(r"^bakar-(detach|dispatch)-[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$")

REGISTRATION_ENV = "BAKAR_DETACH_UNIT"

# The one list of session variables forwarded into a detached unit; both
# bakar.detach and steps/remote_dispatch.py read it from here.
SESSION_FORWARD_VARS: tuple[str, ...] = (
    "SSH_AUTH_SOCK",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "LANG",
    "LC_ALL",
)

_DETACH_PREFIX = "bakar-detach-"

_state: dict[str, str | None] = {"unit": None}


class DetachUnavailable(RuntimeError):  # noqa: N818 - name fixed by the spec
    """No usable runtime directory, so detached records cannot be kept."""


@dataclass(frozen=True)
class UnitPaths:
    log: Path
    sentinel: Path
    registry: Path


def is_unit_name(name: str) -> bool:
    return UNIT_RE.fullmatch(name) is not None


def new_unit_name(now: datetime | None = None) -> str:
    stamp = (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y%m%d-%H%M%S")
    return f"{_DETACH_PREFIX}{stamp}-{secrets.token_hex(3)}"


def runtime_dir() -> Path:
    """Return ``$XDG_RUNTIME_DIR``; never fall back to /tmp or the home directory."""
    raw = os.environ.get("XDG_RUNTIME_DIR", "")
    if not raw:
        raise DetachUnavailable("XDG_RUNTIME_DIR is not set")
    path = Path(raw)
    if not path.is_dir():
        raise DetachUnavailable(f"XDG_RUNTIME_DIR is not a directory: {raw}")
    st = path.stat()
    if st.st_uid != os.getuid():
        raise DetachUnavailable(f"XDG_RUNTIME_DIR is owned by another user: {raw}")
    if st.st_mode & 0o022:
        raise DetachUnavailable(f"XDG_RUNTIME_DIR is group- or world-writable (shared directory): {raw}")
    return path


def unit_paths(unit: str) -> UnitPaths:
    base = runtime_dir()
    return UnitPaths(
        log=base / f"{unit}.log",
        sentinel=base / f"{unit}.log.rc",
        registry=base / f"{unit}.runs",
    )


def read_sentinel(unit: str) -> int | None:
    """Return the unit's recorded exit status, or None when unfinished/unreadable."""
    try:
        return int(unit_paths(unit).sentinel.read_text().strip())
    except OSError, ValueError:
        return None


def append_registration(unit: str, run_id: str, run_dir: str | Path, pid: int) -> bool:
    """Append one registration line; False when the record cannot be written."""
    line = json.dumps({"run_id": run_id, "run_dir": str(run_dir), "pid": pid})
    try:
        with unit_paths(unit).registry.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        return False
    return True


def _read_registry(path: Path) -> list[dict]:
    out: list[dict] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for raw in text.splitlines():
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def read_registrations(unit: str) -> list[dict]:
    """Return the unit's registrations, skipping malformed lines."""
    return _read_registry(unit_paths(unit).registry)


def find_unit_for_run(run_id: str) -> str | None:
    """Return the unit that registered ``run_id``, scanning every ``*.runs``."""
    try:
        base = runtime_dir()
    except DetachUnavailable:
        return None
    for reg in sorted(base.glob("*.runs")):
        unit = reg.name.removesuffix(".runs")
        if not is_unit_name(unit):
            continue
        if any(rec.get("run_id") == run_id for rec in _read_registry(reg)):
            return unit
    return None


def consume_registration_env(environ: dict[str, str] | os._Environ[str] = os.environ) -> None:
    """Pop ``REGISTRATION_ENV`` so children never inherit it.

    The value becomes the current unit only if it is a well-formed unit name;
    anything else (for example a shell fragment) is discarded.
    """
    value = environ.pop(REGISTRATION_ENV, None)
    if value is not None and is_unit_name(value):
        _state["unit"] = value


def current_unit() -> str | None:
    return _state["unit"]


def register_current_run(run_id: str, run_dir: str | Path, pid: int) -> bool:
    """Register a run with the current unit; no-op without one."""
    unit = _state["unit"]
    if unit is None:
        return False
    return append_registration(unit, run_id, run_dir, pid)


def prune(now: float | None = None, max_age_days: int = 7) -> list[str]:
    """Delete records of finished ``bakar-detach-`` units older than the age.

    A unit without a sentinel is still running (or its fate is unknown) and is
    kept; ``bakar-dispatch-`` units are never touched. Returns pruned units.
    """
    try:
        base = runtime_dir()
    except DetachUnavailable:
        return []
    cutoff = (time.time() if now is None else now) - max_age_days * 86400
    pruned: list[str] = []
    for sentinel in sorted(base.glob(f"{_DETACH_PREFIX}*.log.rc")):
        unit = sentinel.name.removesuffix(".log.rc")
        if not is_unit_name(unit) or not unit.startswith(_DETACH_PREFIX):
            continue
        try:
            if sentinel.stat().st_mtime >= cutoff:
                continue
        except OSError:
            continue
        paths = unit_paths(unit)
        for p in (paths.log, paths.sentinel, paths.registry):
            try:
                p.unlink()
            except OSError:
                pass
        pruned.append(unit)
    return pruned
