"""The unprivileged cache-directory remediation for ``bakar setup``.

:class:`CacheDirsAction` remediates the ``cache-dirs`` ``doctor`` check by
``mkdir -p``-ing the sstate / downloads / ccache directories under ``$HOME``.
These are user-owned paths, so the action is always unprivileged - it never
runs under the single ``sudo`` script. ``is_satisfied(profile)`` checks the
filesystem directly (the dirs are not carried on :class:`HostProfile`): True
only when every target dir already exists and is writable.

The default targets mirror the shared-ccache convention in
:func:`bakar.config.shared_ccache_dir` (``~/.cache/bakar/ccache``); the plan
builder may pass explicit paths via the constructor.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from bakar.setup.actions.base import RunCommand, WriteFile

if TYPE_CHECKING:
    from bakar.setup.profile import HostProfile


def _default_cache_dirs() -> list[Path]:
    """The default sstate/downloads/ccache directories under ``$HOME``.

    Anchored on ``XDG_CACHE_HOME`` (falling back to ``~/.cache``) so the
    sstate and downloads caches sit alongside the shared ccache that
    :func:`bakar.config.shared_ccache_dir` already places at
    ``<cache>/bakar/ccache``.
    """
    cache_home = os.environ.get("XDG_CACHE_HOME")
    base = (Path(cache_home) if cache_home else Path.home() / ".cache") / "bakar"
    return [base / "sstate", base / "downloads", base / "ccache"]


class CacheDirsAction:
    """Create the sstate/downloads/ccache directories under ``$HOME``.

    Remediates the ``cache-dirs`` check; always unprivileged because the
    targets live under the user's home. The dirs default to the
    XDG-cache-home layout but may be overridden by the constructor.
    """

    check_name = "cache-dirs"
    needs_root = False

    def __init__(self, dirs: list[Path] | None = None) -> None:
        self.dirs = dirs if dirs is not None else _default_cache_dirs()

    def describe(self) -> str:
        joined = ", ".join(str(d) for d in self.dirs)
        return f"mkdir -p the cache directories: {joined}"

    def is_satisfied(self, _profile: HostProfile) -> bool:
        """True when every target dir already exists and is writable.

        The cache dirs are not carried on :class:`HostProfile`, so this checks
        the live filesystem directly with ``os.access(..., os.W_OK)``.
        """
        return all(d.is_dir() and os.access(d, os.W_OK) for d in self.dirs)

    def operations(self) -> list[RunCommand | WriteFile]:
        return [
            RunCommand(argv=["mkdir", "-p", *(str(d) for d in self.dirs)], needs_root=False),
        ]
