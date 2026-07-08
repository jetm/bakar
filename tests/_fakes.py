"""Shared test doubles hoisted from individual test modules.

Provides a ``subprocess.CompletedProcess`` stand-in, a ``run_shell_capture``
fake factory, and a ``mock_calls`` ordering helper used across the suite.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from unittest.mock import MagicMock


class Completed:
    """Minimal stand-in for ``subprocess.CompletedProcess``."""

    def __init__(self, returncode: int, stdout: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout


def make_fake_run_shell_capture(payloads: list[tuple[str, int]], calls: list[dict]):
    """Return a fake ``run_shell_capture`` that writes payloads and records calls.

    ``payloads`` is a list of ``(text, exit_code)`` in call order.
    ``calls`` accumulates ``{"command": ..., "stdout_path": ...}`` dicts.
    """
    payload_iter = iter(payloads)

    def fake_capture(ctx, command, stdout_path, *, step="kas_shell_capture", python_executable=None):
        text, rc = next(payload_iter)
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stdout_path.write_text(text)
        calls.append({"command": command, "stdout_path": stdout_path})
        return rc

    return fake_capture


def ordered_names(parent: MagicMock) -> list[str]:
    """The top-level attribute names of the recorded calls, in order."""
    return [name.split(".", 1)[0] for name, _args, _kwargs in parent.mock_calls if name]
