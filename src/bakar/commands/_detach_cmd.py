"""Shared front of every ``--detach``: launch in a transient unit, print the handle.

stdout carries exactly one line, the JSON handle. Everything else (reasons,
warnings, log tail, lifecycle hints) goes to stderr. The parent resolves no
workspace or build config; the unit re-runs the command and does that itself.
"""

from __future__ import annotations

import json
import os
import sys
from typing import TYPE_CHECKING, NoReturn

from bakar import detach
from bakar.commands import _hints

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

_LOG_TAIL_LINES = 20


def _log_tail(log: str) -> list[str]:
    try:
        with open(log, encoding="utf-8", errors="replace") as fh:
            return fh.read().splitlines()[-_LOG_TAIL_LINES:]
    except OSError:
        return []


def detach_and_exit(argv: Sequence[str], cwd: Path) -> NoReturn:
    """Launch ``argv`` detached from ``cwd``, print the handle, and exit."""
    request = detach.LaunchRequest(argv=detach.strip_detach(argv), cwd=cwd, environ=os.environ)
    try:
        handle = detach.launch(request)
    except detach.DetachUnavailable as exc:
        print(f"bakar: cannot detach: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    if detach.linger_enabled() is False:
        print(
            "bakar: warning: user lingering is off, so the run stops when you log out; "
            "run `loginctl enable-linger` to keep it alive",
            file=sys.stderr,
        )

    print(json.dumps(handle.to_json()))
    sys.stdout.flush()
    _hints.emit(_hints.after_detach(handle.run_id or handle.unit, handle.run_id))

    if handle.state == "failed":
        for line in _log_tail(handle.log):
            print(line, file=sys.stderr)
        raise SystemExit(handle.exit_code or 1)
    raise SystemExit(0)
