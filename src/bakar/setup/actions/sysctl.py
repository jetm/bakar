"""The privileged sysctl remediation for ``bakar setup``.

:class:`SysctlAction` remediates the ``sysctl`` ``doctor`` check by writing a
removable ``/etc/sysctl.d/99-bakar.conf`` drop-in (never the global
``/etc/sysctl.conf``) and reloading it with ``sysctl --system``. Its
recommended target values are constants that exceed the ``[host]`` config
floors (4096 / 524288 / 20), so applying them satisfies ``doctor``. The action
owns those constants and reads no resolved config.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from bakar.setup.actions.base import RunCommand, WriteFile

if TYPE_CHECKING:
    from bakar.setup.profile import HostProfile

# Recommended target values. These exceed the ``[host]`` config floors
# (inotify_instances 4096, inotify_watches 524288, swappiness_max 20), so a
# host that meets them passes ``check_sysctl``.
RECOMMENDED_INOTIFY_INSTANCES = 8192
RECOMMENDED_INOTIFY_WATCHES = 1048576
RECOMMENDED_SWAPPINESS = 10

_DROPIN_PATH = "/etc/sysctl.d/99-bakar.conf"

_DROPIN_CONTENT = (
    "# Managed by bakar setup. Remove this file to revert.\n"
    f"fs.inotify.max_user_instances = {RECOMMENDED_INOTIFY_INSTANCES}\n"
    f"fs.inotify.max_user_watches = {RECOMMENDED_INOTIFY_WATCHES}\n"
    f"vm.swappiness = {RECOMMENDED_SWAPPINESS}\n"
)


class SysctlAction:
    """Apply the inotify/swappiness sysctl knobs via a removable drop-in."""

    check_name = "sysctl"
    needs_root = True

    def describe(self) -> str:
        return (
            f"write {_DROPIN_PATH} "
            f"(fs.inotify.max_user_instances={RECOMMENDED_INOTIFY_INSTANCES}, "
            f"fs.inotify.max_user_watches={RECOMMENDED_INOTIFY_WATCHES}, "
            f"vm.swappiness={RECOMMENDED_SWAPPINESS}) and run sysctl --system"
        )

    def is_satisfied(self, profile: HostProfile) -> bool:
        """True when the live values already meet the recommended targets.

        A ``None`` live value means the knob was unreadable; that is treated as
        not satisfied so the remediation still runs.
        """
        if profile.inotify_instances is None or profile.inotify_watches is None or profile.swappiness is None:
            return False
        return (
            profile.inotify_instances >= RECOMMENDED_INOTIFY_INSTANCES
            and profile.inotify_watches >= RECOMMENDED_INOTIFY_WATCHES
            and profile.swappiness <= RECOMMENDED_SWAPPINESS
        )

    def operations(self) -> list[RunCommand | WriteFile]:
        return [
            WriteFile(
                path=_DROPIN_PATH,
                content=_DROPIN_CONTENT,
                needs_root=True,
                backup=False,
            ),
            RunCommand(argv=["sysctl", "--system"], needs_root=True),
        ]
