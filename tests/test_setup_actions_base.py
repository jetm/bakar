"""Tests for the ``Action`` protocol and operation primitives.

Covers the interface contract from ``bakar.setup.actions.base``: an Action
exposes the ``check_name`` it remediates and a ``needs_root`` flag, a
``WriteFile`` carries a backup-before-write flag, and privileged vs
unprivileged operations are distinguishable via ``needs_root``.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import TYPE_CHECKING

import pytest

from bakar.setup.actions.base import Action, RunCommand, WriteFile

if TYPE_CHECKING:
    from bakar.setup.profile import HostProfile


class _FakeAction:
    """A minimal Action that remediates a named check via one op."""

    check_name = "sysctl"
    needs_root = True

    def describe(self) -> str:
        return "write /etc/sysctl.d/99-bakar.conf"

    def is_satisfied(self, _profile: HostProfile) -> bool:
        return False

    def operations(self) -> list[RunCommand | WriteFile]:
        return [
            WriteFile(path="/etc/sysctl.d/99-bakar.conf", content="x", needs_root=True, backup=False),
            RunCommand(argv=["sysctl", "--system"], needs_root=True),
        ]


def test_action_exposes_check_name_and_needs_root() -> None:
    """An Action carries the check_name it remediates and a needs_root flag."""
    action = _FakeAction()
    assert isinstance(action, Action)
    assert action.check_name == "sysctl"
    assert action.needs_root is True


def test_write_file_carries_backup_flag() -> None:
    """WriteFile can represent a backup-before-write operation."""
    with_backup = WriteFile(path="/etc/docker/daemon.json", content="{}", needs_root=True, backup=True)
    without_backup = WriteFile(path="/etc/sysctl.d/99-bakar.conf", content="x", needs_root=True, backup=False)
    assert with_backup.backup is True
    assert without_backup.backup is False


def test_privileged_and_unprivileged_ops_are_distinguishable() -> None:
    """needs_root separates the privileged ops from the unprivileged ones."""
    privileged: list[RunCommand | WriteFile] = [
        RunCommand(argv=["systemctl", "enable", "--now", "docker"], needs_root=True),
        WriteFile(path="/etc/sysctl.d/99-bakar.conf", content="x", needs_root=True, backup=False),
    ]
    unprivileged: list[RunCommand | WriteFile] = [
        RunCommand(argv=["uv", "tool", "install", "kas"], needs_root=False),
        WriteFile(path="/home/user/.cache/bakar", content="", needs_root=False, backup=False),
    ]
    assert all(op.needs_root for op in privileged)
    assert not any(op.needs_root for op in unprivileged)


def test_operations_are_frozen() -> None:
    """The operation primitives are immutable value objects."""
    cmd = RunCommand(argv=["docker", "pull", "img"], needs_root=False)
    with pytest.raises(FrozenInstanceError):
        cmd.needs_root = True  # type: ignore[misc]
