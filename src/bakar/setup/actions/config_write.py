"""Persist the applied host-knob values into the global ``[host]`` config.

After ``bakar setup`` applies the system knobs (sysctl inotify/swappiness,
docker daemon nofile), it records the applied target values into the
**user-global** config (``~/.config/bakar/config.toml``) so a follow-up
``bakar doctor`` verifies the machine against what setup applied. This action
owns that persist step.

It writes ONLY the global config via :func:`user_config.set_setting` (path
default); it never touches a workspace ``.bakar.toml``. The dotted keys are the
section-relative spelling - ``host.inotify_instances``, not the field-name
``host.host_inotify_instances`` - because ``[host]`` is already the section and
``set_setting`` would reject the doubled-prefix form. Memory (``mem_min_gb``) is
advisory and never applied, so it is never persisted here.

Unlike the remediation actions, this carries no shell ``operations()``: it
persists directly in :meth:`apply`, which the runner calls in the user context.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from bakar.setup.actions.docker import NOFILE_SOFT
from bakar.setup.actions.sysctl import (
    RECOMMENDED_INOTIFY_INSTANCES,
    RECOMMENDED_INOTIFY_WATCHES,
    RECOMMENDED_SWAPPINESS,
)
from bakar.user_config import get_setting, set_setting

if TYPE_CHECKING:
    from pathlib import Path

    from bakar.setup.actions.base import RunCommand, WriteFile
    from bakar.setup.profile import HostProfile

# Per-check host knobs: maps each value-applying check name to the settings it
# controls. ConfigWriteAction uses this to write only the knobs that correspond
# to the checks that actually ran, so a docker-ulimits-only run does not
# erroneously write sysctl values (and vice versa).
_CHECK_SETTINGS: dict[str, dict[str, int]] = {
    "sysctl": {
        "host.inotify_instances": RECOMMENDED_INOTIFY_INSTANCES,
        "host.inotify_watches": RECOMMENDED_INOTIFY_WATCHES,
        "host.swappiness_max": RECOMMENDED_SWAPPINESS,
    },
    "docker-ulimits": {
        "host.nofile_soft": NOFILE_SOFT,
    },
}

# All host knobs across both checks; kept for backward compatibility with tests
# that reference this dict directly and for is_satisfied on a default instance.
APPLIED_HOST_SETTINGS: dict[str, int] = {
    **_CHECK_SETTINGS["sysctl"],
    **_CHECK_SETTINGS["docker-ulimits"],
}

_ALL_CHECKS = frozenset(_CHECK_SETTINGS)


class ConfigWriteAction:
    """Persist the applied host-knob values to the global ``[host]`` config.

    ``applied_checks`` names the value-applying checks (``sysctl`` and/or
    ``docker-ulimits``) that ran; only their corresponding knobs are written.
    Defaults to both checks for backward compatibility with callers that do not
    track which checks ran.

    Carries no doctor-check remediation of its own; ``check_name`` is the
    synthetic ``"host-config-persist"`` the plan builder appends when any
    value-applying action is in the plan. Unprivileged: ``set_setting`` writes a
    file under ``$HOME``, never a system path.
    """

    check_name = "host-config-persist"
    needs_root = False

    def __init__(self, applied_checks: frozenset[str] = _ALL_CHECKS) -> None:
        self._settings: dict[str, int] = {}
        for check in _CHECK_SETTINGS:  # iterate in insertion order for deterministic key sequence
            if check in applied_checks:
                self._settings.update(_CHECK_SETTINGS[check])

    def describe(self) -> str:
        return "record applied host knobs in the global [host] config"

    def is_satisfied(self, _profile: HostProfile) -> bool:
        """True when every applicable knob is already set to its target in config.

        Checks the global config (``set_setting``'s default path) for each
        dotted key. When all already equal their target, the persist is a
        no-op and the plan builder drops the action - keeping a prepared host's
        plan empty.
        """
        return all(get_setting(key) == value for key, value in self._settings.items())

    def operations(self) -> list[RunCommand | WriteFile]:
        """No shell ops: the persist happens in :meth:`apply`, not via primitives."""
        return []

    def apply(self, path: Path | None = None) -> None:
        """Write each applied knob to the global ``[host]`` config.

        ``set_setting`` coerces from a string, so each value is passed as
        ``str(value)``. ``path`` defaults to ``None``, which routes
        ``set_setting`` to the user-global ``~/.config/bakar/config.toml``;
        tests pass an explicit path to assert the global-config target.
        """
        for key, value in self._settings.items():
            set_setting(key, str(value), path)
