"""The git-identity remediation for ``bakar setup``.

:class:`GitConfigAction` remediates the ``git-global-config`` ``doctor`` check by
setting ``user.email`` and ``user.name`` with ``git config`` - deliberately NOT
``--global``. The developer keeps separate identities per project tree
(``~/repos/work`` vs ``~/repos/personal``) via ``includeIf "gitdir:..."``
conditionals, so a global write is the wrong place. ``check_git_global_config``
probes the identity from a sub-repo under the workspace (where the conditional
fires); this action writes with ``git -C <probe_dir>`` so the value lands exactly
where the check reads it, and a non-``--global`` write outside a repo does not
abort. The email and name are supplied by the constructor - the command resolves
them from CLI options or an interactive prompt. The action is unprivileged.

The live git identity is not carried on :class:`HostProfile`, so ``is_satisfied``
reads it directly with the same ``git -C <probe_dir> config`` the check uses.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

from bakar.setup.actions.base import RunCommand, WriteFile

if TYPE_CHECKING:
    from bakar.setup.profile import HostProfile


def _read(key: str, cwd: str | None = None) -> str | None:
    """Return the git value for ``key`` or ``None`` when unset.

    Reads with ``git -C <cwd> config`` when ``cwd`` is given - matching how
    ``check_git_global_config`` probes a sub-repo so ``includeIf`` conditionals
    resolve the right per-tree identity - else plain ``git config``. A non-zero
    exit, a missing ``git`` binary, or an empty value all read as unset.
    """
    argv = ["git", "-C", cwd, "config", key] if cwd else ["git", "config", key]
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=5, check=False)
    except FileNotFoundError, subprocess.TimeoutExpired:
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


class GitConfigAction:
    """Set the git ``user.email``/``user.name`` identity where the check reads it.

    The email and name come from constructor arguments (the command supplies
    them from CLI options or a prompt). ``probe_dir`` is the directory
    ``check_git_global_config`` probes (a sub-repo under the workspace); when
    given, the reads and writes run with ``git -C <probe_dir>`` so the write
    lands in that repo's config exactly where the check reads it, honouring
    per-tree ``includeIf`` identities without touching ``--global``. Unprivileged.
    """

    check_name = "git-global-config"
    needs_root = False

    def __init__(self, email: str, name: str, probe_dir: str | None = None) -> None:
        if "\n" in email or "\r" in email or "\n" in name or "\r" in name:
            raise ValueError(f"git identity values must not contain newlines: email={email!r}, name={name!r}")
        self.email = email
        self.name = name
        self.probe_dir = probe_dir

    def describe(self) -> str:
        where = f" in {self.probe_dir}" if self.probe_dir else ""
        return f"set git identity (user.email={self.email}, user.name={self.name}){where}"

    def is_satisfied(self, _profile: HostProfile) -> bool:
        """True when both git identities already resolve in the probe dir.

        Reads the live values directly because the identity is not carried on
        :class:`HostProfile`.
        """
        return _read("user.email", self.probe_dir) is not None and _read("user.name", self.probe_dir) is not None

    def operations(self) -> list[RunCommand | WriteFile]:
        prefix = ["git", "-C", self.probe_dir] if self.probe_dir else ["git"]
        return [
            RunCommand(argv=[*prefix, "config", "user.email", self.email], needs_root=False),
            RunCommand(argv=[*prefix, "config", "user.name", self.name], needs_root=False),
        ]
