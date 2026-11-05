"""Persist the settings a selected feature set implies, in one atomic write.

``bakar setup init`` turns the user's feature choices into config changes
(keys to set, keys to drop). This action carries them and applies them through
:func:`user_config.apply_settings` so the whole change lands in one write or not
at all.

It has no shell ``operations()``: the runner calls :meth:`apply` after every
operation has run, so the settings are persisted only once the host operations
succeeded. A privileged script that fails raises before :meth:`apply` is reached
and leaves the config untouched.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from bakar.user_config import apply_settings

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from bakar.setup.actions.base import RunCommand, WriteFile
    from bakar.setup.profile import HostProfile


class FeatureConfigAction:
    """Apply a feature selection's config ``sets`` and ``unsets`` atomically.

    ``path`` defaults to ``None``, which routes ``apply_settings`` to the
    user-global ``~/.config/bakar/config.toml``; tests pass an explicit path.
    Unprivileged: the write lands under ``$HOME``, never a system path.
    """

    check_name = "feature-config"
    needs_root = False

    def __init__(
        self,
        sets: Mapping[str, str],
        unsets: Sequence[str],
        path: Path | None = None,
    ) -> None:
        self._sets = dict(sets)
        self._unsets = list(unsets)
        self._path = path

    def describe(self) -> str:
        parts = [f"set {key} = {value}" for key, value in sorted(self._sets.items())]
        parts.extend(f"unset {key}" for key in sorted(self._unsets))
        return "record feature settings in the global config: " + ", ".join(parts)

    def is_satisfied(self, _profile: HostProfile) -> bool:
        """True only when there is nothing to set or unset."""
        return not self._sets and not self._unsets

    def operations(self) -> list[RunCommand | WriteFile]:
        """No shell ops: the persist happens in :meth:`apply`."""
        return []

    def apply(self) -> None:
        """Write every set and unset with one ``apply_settings`` call."""
        apply_settings(self._sets, self._unsets, self._path)
