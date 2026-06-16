"""Render the privileged ``bakar setup`` operations into an auditable script.

:func:`render_script` turns the ``needs_root`` operations of the planned
actions into a single ``bakar-host-setup.sh`` string - the audit artifact the
runner pipes to ``sudo bash -s`` via stdin. The script starts with
``set -euo pipefail`` and contains *only* privileged operations; unprivileged
operations run inline in the user context and never reach this function.

The renderer understands the two operation primitives from
:mod:`bakar.setup.actions.base`:

- :class:`RunCommand` -> its ``argv`` shell-quoted per argument with
  :func:`shlex.quote`, so the embedded ``python3 -c`` daemon.json merge script
  (with its newlines and quotes) survives verbatim.
- :class:`WriteFile` -> an optional existence-guarded ``cp`` backup followed by
  a quoted heredoc that writes the content, robust for multi-line files.

It never emits a ``curl``-piped-to-shell line: it renders only the literal
operations the actions produced.
"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bakar.setup.actions.base import RunCommand, WriteFile

_SCRIPT_NAME = "bakar-host-setup.sh"

_HEREDOC_DELIM = "BAKAR_EOF"


def _render_run_command(op: RunCommand) -> str:
    return " ".join(shlex.quote(arg) for arg in op.argv)


def _render_write_file(op: WriteFile) -> str:
    quoted = shlex.quote(op.path)
    lines: list[str] = []
    if op.backup:
        # Only copy when the target already exists, so a fresh host does not
        # fail on a missing source.
        lines.append(f"if [ -e {quoted} ]; then cp {quoted} {quoted}.bak; fi")
    # A quoted heredoc delimiter disables variable/command expansion, so the
    # content lands verbatim regardless of '$' or backticks inside it.
    content = op.content if op.content.endswith("\n") else op.content + "\n"
    lines.append(f"cat > {quoted} <<'{_HEREDOC_DELIM}'")
    lines.append(content.rstrip("\n"))
    lines.append(_HEREDOC_DELIM)
    return "\n".join(lines)


def render_script(operations: list[RunCommand | WriteFile]) -> str:
    """Render the privileged ``operations`` into a bash script string.

    Only ``needs_root`` operations are emitted; any unprivileged operation in
    the input is silently dropped because it runs inline, not under ``sudo``.
    The script always begins with the shebang and ``set -euo pipefail``.
    """
    body: list[str] = ["#!/usr/bin/env bash", "set -euo pipefail", ""]
    for op in operations:
        if not op.needs_root:
            continue
        if hasattr(op, "argv"):
            body.append(_render_run_command(op))  # type: ignore[arg-type]
        else:
            body.append(_render_write_file(op))  # type: ignore[arg-type]
    return "\n".join(body) + "\n"
