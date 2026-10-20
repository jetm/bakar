"""Output-mode resolution for bakar's human-facing rendering.

``bakar build`` and ``bakar monitor`` render either a Rich ``Live`` display (interactive
terminals) or plain, greppable, ANSI-free text (pipes / CI logs). This module owns the
policy that picks between them. It lives at the package top level so both the command
tier (``commands/``) and the step tier (``steps/``) can import it without a layer
violation (``steps/`` cannot import ``commands/``).
"""

from __future__ import annotations

import select
from enum import StrEnum


class OutputMode(StrEnum):
    """How bakar renders human-facing progress output."""

    RICH = "rich"
    PLAIN = "plain"


def _ci_env_is_truthy(ci_env: str | None) -> bool:
    """Return True when a ``CI`` environment marker should force plain output.

    Presence is truthy except the explicit false-y spellings ``""``, ``"0"``, and
    ``"false"`` (case-insensitive) that some shells set to disable CI detection.
    """
    if ci_env is None:
        return False
    return ci_env.strip().lower() not in ("", "0", "false")


def resolve_output_mode(
    override: OutputMode | None,
    *,
    isatty: bool,
    ci_env: str | None,
) -> OutputMode:
    """Resolve the effective output mode.

    An explicit ``override`` always wins. Otherwise the mode is
    :attr:`OutputMode.PLAIN` when the human-output stream is not a TTY or a truthy
    ``CI`` marker is present, and :attr:`OutputMode.RICH` on an interactive terminal.
    Pure by design (takes ``isatty``/``ci_env`` as arguments rather than calling
    ``sys``/``os``) so callers stay testable.
    """
    if override is not None:
        return override
    if not isatty or _ci_env_is_truthy(ci_env):
        return OutputMode.PLAIN
    return OutputMode.RICH


def stdout_reader_gone(fd: int = 1) -> bool:
    """Return True when nothing is left to read what ``fd`` writes.

    The write end of a pipe or socket reports ``POLLERR``/``POLLHUP`` once its
    reader closes, so a follower idling on a quiet log can notice a consumer
    that quit (``| head``) or an ssh client that died without writing a probe
    byte into the stream. Terminals, regular files and ``/dev/null`` keep
    reporting writable and are never "gone". Returns False where the platform
    has no ``poll`` or the descriptor cannot be polled.
    """
    poll_factory = getattr(select, "poll", None)
    if poll_factory is None:
        return False
    try:
        poller = poll_factory()
        poller.register(fd, select.POLLOUT)
        events = poller.poll(0)
    except OSError, ValueError:
        return False
    gone = select.POLLERR | select.POLLHUP | select.POLLNVAL
    return any(mask & gone for _fd, mask in events)
