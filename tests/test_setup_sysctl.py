"""Tests for the privileged sysctl remediation action.

Covers that :class:`SysctlAction` writes the removable
``/etc/sysctl.d/99-bakar.conf`` drop-in (never the global ``/etc/sysctl.conf``)
plus ``sysctl --system``, and that ``is_satisfied`` reflects the recommended
target constants (instances >= 8192, watches >= 1048576, swappiness <= 10),
treating an unreadable (``None``) live value as not satisfied.
"""

from __future__ import annotations

from bakar.setup.actions.base import Action, RunCommand, WriteFile
from bakar.setup.actions.sysctl import (
    RECOMMENDED_INOTIFY_INSTANCES,
    RECOMMENDED_INOTIFY_WATCHES,
    RECOMMENDED_SWAPPINESS,
    SysctlAction,
)
from tests.conftest import make_host_profile


def test_sysctl_action_conforms_to_protocol() -> None:
    """SysctlAction satisfies the Action protocol and remediates ``sysctl``."""
    action = SysctlAction()
    assert isinstance(action, Action)
    assert action.check_name == "sysctl"
    assert action.needs_root is True


def test_recommended_constants_exceed_config_floors() -> None:
    """The recommended targets clear the [host] floors of 4096/524288/20."""
    assert RECOMMENDED_INOTIFY_INSTANCES == 8192
    assert RECOMMENDED_INOTIFY_WATCHES == 1048576
    assert RECOMMENDED_SWAPPINESS == 10
    assert RECOMMENDED_INOTIFY_INSTANCES > 4096
    assert RECOMMENDED_INOTIFY_WATCHES > 524288
    assert RECOMMENDED_SWAPPINESS < 20


def test_operations_write_dropin_not_global_sysctl_conf() -> None:
    """The write targets the 99-bakar.conf drop-in, never /etc/sysctl.conf."""
    ops = SysctlAction().operations()
    writes = [op for op in ops if isinstance(op, WriteFile)]
    assert len(writes) == 1
    write = writes[0]
    assert write.path == "/etc/sysctl.d/99-bakar.conf"
    assert write.path != "/etc/sysctl.conf"
    assert write.needs_root is True


def test_dropin_content_carries_recommended_values() -> None:
    """The drop-in content sets the three recommended sysctl values."""
    write = next(op for op in SysctlAction().operations() if isinstance(op, WriteFile))
    assert "fs.inotify.max_user_instances = 8192" in write.content
    assert "fs.inotify.max_user_watches = 1048576" in write.content
    assert "vm.swappiness = 10" in write.content


def test_operations_reload_with_sysctl_system() -> None:
    """A privileged ``sysctl --system`` reload follows the drop-in write."""
    ops = SysctlAction().operations()
    reload_cmds = [op for op in ops if isinstance(op, RunCommand)]
    assert reload_cmds == [RunCommand(argv=["sysctl", "--system"], needs_root=True)]


def test_is_satisfied_when_all_live_values_meet_targets() -> None:
    """Live values at exactly the targets satisfy the action."""
    assert SysctlAction().is_satisfied(make_host_profile()) is True


def test_is_satisfied_when_values_exceed_targets() -> None:
    """Higher inotify and lower swappiness still satisfy the action."""
    profile = make_host_profile(inotify_instances=16384, inotify_watches=2097152, swappiness=1)
    assert SysctlAction().is_satisfied(profile) is True


def test_not_satisfied_when_instances_below_target() -> None:
    """Live inotify instances below 8192 leaves the action unsatisfied."""
    profile = make_host_profile(inotify_instances=4096)
    assert SysctlAction().is_satisfied(profile) is False


def test_not_satisfied_when_watches_below_target() -> None:
    """Live inotify watches below 1048576 leaves the action unsatisfied."""
    profile = make_host_profile(inotify_watches=524288)
    assert SysctlAction().is_satisfied(profile) is False


def test_not_satisfied_when_swappiness_above_target() -> None:
    """A live swappiness above 10 leaves the action unsatisfied."""
    profile = make_host_profile(swappiness=20)
    assert SysctlAction().is_satisfied(profile) is False


def test_not_satisfied_when_a_live_value_is_unreadable() -> None:
    """A None (unreadable) live knob is treated as not satisfied."""
    assert SysctlAction().is_satisfied(make_host_profile(inotify_instances=None)) is False
    assert SysctlAction().is_satisfied(make_host_profile(inotify_watches=None)) is False
    assert SysctlAction().is_satisfied(make_host_profile(swappiness=None)) is False
