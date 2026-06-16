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

from bakar.user_config import get_setting, set_setting

if TYPE_CHECKING:
    from pathlib import Path

    from bakar.setup.actions.base import RunCommand, WriteFile
    from bakar.setup.profile import HostProfile

# The four host knobs setup actually applies, mapped to the section-relative
# dotted setting key (NOT the ``host_*`` field name). These exceed the doctor
# floors, so persisting them lets the config-reading checks PASS. ``mem_min_gb``
# is intentionally absent: memory is advisory, never applied, so never recorded.
APPLIED_HOST_SETTINGS: dict[str, int] = {
    "host.inotify_instances": 8192,
    "host.inotify_watches": 1048576,
    "host.swappiness_max": 10,
    "host.nofile_soft": 65536,
}


class ConfigWriteAction:
    """Persist the applied host-knob values to the global ``[host]`` config.

    Carries no doctor-check remediation of its own; ``check_name`` is the
    synthetic ``"host-config-persist"`` the plan builder appends when any
    value-applying action is in the plan. Unprivileged: ``set_setting`` writes a
    file under ``$HOME``, never a system path.
    """

    check_name = "host-config-persist"
    needs_root = False

    def describe(self) -> str:
        return "record applied host knobs in the global [host] config"

    def is_satisfied(self, _profile: HostProfile) -> bool:
        """True when every applied knob is already set to its target in config.

        Checks the global config (``set_setting``'s default path) for each
        dotted key. When all four already equal their target, the persist is a
        no-op and the plan builder drops the action - keeping a prepared host's
        plan empty.
        """
        return all(get_setting(key) == value for key, value in APPLIED_HOST_SETTINGS.items())

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
        for key, value in APPLIED_HOST_SETTINGS.items():
            set_setting(key, str(value), path)
