"""The unprivileged cache-directory remediation for ``bakar setup``.

:class:`CacheDirsAction` remediates the ``cache-dirs`` ``doctor`` check, which
FAILs only when ``SSTATE_DIR`` / ``DL_DIR`` are exported but point at a missing
or non-writable directory. The action ``mkdir -p``s exactly those configured
paths - reading the same environment variables as
:func:`bakar.diagnostics.check_cache_dirs` - so applying it actually clears the
check it is mapped to. They are user-owned paths, so the action is always
unprivileged and never runs under the single ``sudo`` script.
``is_satisfied(profile)`` checks the filesystem directly (the dirs are not
carried on :class:`HostProfile`): True when every configured dir already exists
and is writable, and trivially True when neither variable is set (nothing to
create).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from bakar.setup.actions.base import RunCommand, WriteFile

if TYPE_CHECKING:
    from bakar.setup.profile import HostProfile


def _configured_cache_dirs() -> list[Path]:
    """The ``SSTATE_DIR`` / ``DL_DIR`` paths the ``cache-dirs`` check examines.

    Reads the same environment variables as
    :func:`bakar.diagnostics.check_cache_dirs`, so the remediation targets
    exactly the directories whose absence makes that check FAIL. An unset
    variable contributes nothing - the check passes when neither is set, so
    there is nothing to create.
    """
    dirs: list[Path] = []
    for env in ("SSTATE_DIR", "DL_DIR"):
        value = os.environ.get(env)
        if value:
            dirs.append(Path(value))
    return dirs


class CacheDirsAction:
    """Create the configured ``SSTATE_DIR`` / ``DL_DIR`` cache directories.

    Remediates the ``cache-dirs`` check; always unprivileged because the targets
    live in user-owned space. The dirs default to the exported ``SSTATE_DIR`` /
    ``DL_DIR`` paths the check inspects but may be overridden by the constructor.
    """

    check_name = "cache-dirs"
    needs_root = False

    def __init__(self, dirs: list[Path] | None = None) -> None:
        self.dirs = dirs if dirs is not None else _configured_cache_dirs()

    def describe(self) -> str:
        joined = ", ".join(str(d) for d in self.dirs) or "(none configured)"
        return f"mkdir -p the configured cache directories: {joined}"

    def is_satisfied(self, _profile: HostProfile) -> bool:
        """True when every configured dir already exists and is writable.

        The cache dirs are not carried on :class:`HostProfile`, so this checks
        the live filesystem directly with ``os.access(..., os.W_OK)``. Returns
        True when no dir is configured (``all`` over an empty list) - there is
        nothing to create.
        """
        return all(d.is_dir() and os.access(d, os.W_OK) for d in self.dirs)

    def operations(self) -> list[RunCommand | WriteFile]:
        if not self.dirs:
            return []
        return [
            RunCommand(argv=["mkdir", "-p", *(str(d) for d in self.dirs)], needs_root=False),
        ]
