"""Launch a bakar invocation as a transient systemd user unit.

``--detach`` starts the same invocation, minus ``--detach``, in a
``systemd-run --user`` unit so it outlives the caller. The unit writes its
output to a log and its exit status to a sentinel in the user runtime
directory (:mod:`bakar.detach_records`); the launcher then waits a bounded time
for the front-end to register its first run id and returns a :class:`Handle`.

There is no foreground fallback: when a unit cannot be created the launch
raises :class:`DetachUnavailable` and nothing runs.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from bakar import build_scope, detach_records
from bakar.detach_records import REGISTRATION_ENV, SESSION_FORWARD_VARS, DetachUnavailable, UnitPaths

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from pathlib import Path

__all__ = [
    "DetachUnavailable",
    "Handle",
    "LaunchRequest",
    "build_unit_argv",
    "check_available",
    "escape_for_systemd_run",
    "forwarded_env",
    "launch",
    "linger_enabled",
    "strip_detach",
]

_LAUNCH_TIMEOUT_SECS = 30
_LINGER_TIMEOUT_SECS = 5
_POLL_INTERVAL_SECS = 0.2

_FORWARDED_PREFIXES = ("BAKAR_", "KAS_")
_FORWARDED_EXACT = ("PATH", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", *SESSION_FORWARD_VARS)


@dataclass(frozen=True)
class LaunchRequest:
    argv: Sequence[str]
    cwd: Path
    environ: Mapping[str, str]


@dataclass(frozen=True)
class Handle:
    run_id: str | None
    unit: str
    host: str | None
    log: str
    state: str
    exit_code: int | None = None

    def to_json(self) -> dict[str, str | None]:
        """Return the stdout handle: exactly these five keys, nothing else."""
        return {
            "run_id": self.run_id,
            "unit": self.unit,
            "host": self.host,
            "log": self.log,
            "state": self.state,
        }


def strip_detach(argv: Sequence[str]) -> list[str]:
    """Return ``argv`` without any ``--detach`` token."""
    return [a for a in argv if a != "--detach"]


def forwarded_env(environ: Mapping[str, str]) -> dict[str, str]:
    """Return the allowlisted subset of ``environ`` the unit receives.

    ``BAKAR_DETACH_UNIT`` is excluded: the launcher sets it itself, so a value
    inherited from an enclosing detached run must never reach the new unit.
    """
    out: dict[str, str] = {}
    for name, value in environ.items():
        if name == REGISTRATION_ENV:
            continue
        if name.startswith(_FORWARDED_PREFIXES) or name in _FORWARDED_EXACT:
            out[name] = value
    return out


def escape_for_systemd_run(command: str) -> str:
    """Double every ``$`` so systemd hands ``command`` to the shell unchanged.

    systemd expands ``${VAR}`` and collapses ``$$`` in a unit's command line,
    even inside shell quotes, so a user argv token or path carrying either would
    be silently rewritten. ``$$`` is the escape on every systemd version, unlike
    ``--expand-environment=no`` (254+).
    """
    return command.replace("$", "$$")


def build_unit_argv(unit: str, request: LaunchRequest, paths: UnitPaths) -> list[str]:
    """Return the ``systemd-run`` argv that runs ``request`` as ``unit``.

    The wrapper writes the sentinel last, after the log redirection has closed,
    so the sentinel's presence means the log is complete. Every interpolated
    value is shlex-quoted: the log path sits under ``$XDG_RUNTIME_DIR`` and the
    argv is user input. The payload is then ``$``-escaped for systemd itself.
    """
    # -P keeps the unit's working directory off sys.path, so a stray yaml.py or
    # typer.py in the user's cwd cannot shadow the real modules under ``-m``.
    front_end = shlex.join([sys.executable, "-P", "-m", "bakar", *request.argv])
    log = shlex.quote(str(paths.log))
    sentinel = shlex.quote(str(paths.sentinel))
    wrapped = f"{front_end} >{log} 2>&1; rc=$?; printf '%s\\n' \"$rc\" >{sentinel}; exit $rc"
    env = forwarded_env(request.environ)
    setenv = [f"--setenv={name}={env[name]}" for name in sorted(env)]
    setenv.append(f"--setenv={REGISTRATION_ENV}={unit}")
    return [
        "systemd-run",
        "--user",
        f"--unit={unit}",
        "--collect",
        "--quiet",
        # Type=exec makes systemd-run exit non-zero when the unit cannot exec
        # (a missing working directory, say); the default Type=simple reports
        # success and --collect then removes the unit without a trace.
        "--property=Type=exec",
        f"--working-directory={request.cwd}",
        *setenv,
        "--",
        "bash",
        "-c",
        escape_for_systemd_run(wrapped),
    ]


def check_available() -> None:
    """Raise :class:`DetachUnavailable` unless a user unit can be created."""
    if shutil.which("systemd-run") is None:
        raise DetachUnavailable("systemd-run not found")
    try:
        detach_records.runtime_dir()
    except DetachUnavailable as exc:
        raise DetachUnavailable(f"XDG_RUNTIME_DIR unset or unusable ({exc})") from exc
    if not build_scope.systemd_run_available():
        raise DetachUnavailable("user manager unreachable (systemd-run --user failed)")


def linger_enabled() -> bool | None:
    """Return the caller's ``Linger`` setting, or None when it cannot be read."""
    try:
        result = subprocess.run(
            ["loginctl", "show-user", str(os.getuid()), "-p", "Linger", "--value"],
            capture_output=True,
            text=True,
            timeout=_LINGER_TIMEOUT_SECS,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() == "yes"


def _first_run_id(registrations: Sequence[Mapping[str, object]]) -> str | None:
    """Return the first registered ``run_id`` that is a non-empty string."""
    for reg in registrations:
        run_id = reg.get("run_id")
        if isinstance(run_id, str) and run_id:
            return run_id
    return None


def launch(
    request: LaunchRequest,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    bound: float = 60.0,
) -> Handle:
    """Start ``request`` in a transient unit and wait up to ``bound`` seconds.

    Returns ``failed``/``succeeded`` as soon as the sentinel appears,
    ``running`` once a run id registers, and ``starting`` (run id None) when
    the bound expires first. Raises :class:`DetachUnavailable` when no unit
    could be created; never runs the build in the foreground.
    """
    detach_records.prune()
    check_available()
    unit = detach_records.new_unit_name()
    paths = detach_records.unit_paths(unit)
    argv = build_unit_argv(unit, request, paths)
    try:
        result = runner(argv, capture_output=True, text=True, timeout=_LAUNCH_TIMEOUT_SECS, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise DetachUnavailable(f"systemd-run failed to start the unit: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or "").strip()
        raise DetachUnavailable(f"systemd-run exited {result.returncode}: {detail}".rstrip(": "))

    log = str(paths.log)
    deadline = clock() + bound
    while True:
        code = detach_records.read_sentinel(unit)
        if code is not None:
            state = "succeeded" if code == 0 else "failed"
            regs = detach_records.read_registrations(unit)
            return Handle(_first_run_id(regs), unit, None, log, state, code)
        run_id = _first_run_id(detach_records.read_registrations(unit))
        if run_id is not None:
            return Handle(run_id, unit, None, log, "running")
        if clock() >= deadline:
            return Handle(None, unit, None, log, "starting")
        sleep(_POLL_INTERVAL_SECS)
