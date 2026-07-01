"""The git-identity remediation for ``bakar setup``.

:class:`GitConfigAction` remediates the ``git-global-config`` ``doctor`` check
by setting ``user.email`` and ``user.name`` with ``git config`` (no
``--global``), matching how ``check_git_global_config`` in
:mod:`bakar.diagnostics` reads them. The email and name are supplied by the
constructor - the command resolves them from CLI options or an interactive
prompt. The action is unprivileged.

The live git identity is not carried on :class:`HostProfile`, so
``is_satisfied`` reads it directly with ``git config``, mirroring how
``check_git_global_config`` reads it.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

from bakar.setup.actions.base import RunCommand, WriteFile

if TYPE_CHECKING:
    from bakar.setup.profile import HostProfile


def _read(key: str) -> str | None:
    """Return the git value for ``key`` or ``None`` when unset.

    Mirrors ``check_git_global_config`` in :mod:`bakar.diagnostics`: a non-zero
    exit, a missing ``git`` binary, or an empty value all read as unset.
    """
    try:
        out = subprocess.run(
            ["git", "config", key],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except FileNotFoundError, subprocess.TimeoutExpired:
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


class GitConfigAction:
    """Set the global git ``user.email``/``user.name`` identity.

    The email and name come from constructor arguments (the command supplies
    them from CLI options or a prompt); this action never reads resolved
    config. Remediates the ``git-global-config`` check with ``git config`` (no
    ``--global``); unprivileged.
    """

    check_name = "git-global-config"
    needs_root = False

    def __init__(self, email: str, name: str) -> None:
        if "\n" in email or "\r" in email or "\n" in name or "\r" in name:
            raise ValueError(f"git identity values must not contain newlines: email={email!r}, name={name!r}")
        self.email = email
        self.name = name

    def describe(self) -> str:
        return f"set git identity (user.email={self.email}, user.name={self.name})"

    def is_satisfied(self, _profile: HostProfile) -> bool:
        """True when both git identities are already set.

        Reads the live values directly because the identity is not carried on
        :class:`HostProfile`.
        """
        return _read("user.email") is not None and _read("user.name") is not None

    def operations(self) -> list[RunCommand | WriteFile]:
        return [
            RunCommand(argv=["git", "config", "user.email", self.email], needs_root=False),
            RunCommand(argv=["git", "config", "user.name", self.name], needs_root=False),
        ]
