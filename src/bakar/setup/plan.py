"""The :class:`SetupPlan` builder for ``bakar setup``.

:func:`build` resolves one ``cfg`` (mirroring ``commands/doctor.py``'s dispatch),
runs the host-environment ``doctor`` checks, maps each host-scoped check that
FAILed to its remediation :class:`~bakar.setup.actions.base.Action` by
``check_name``, and drops any action the live :class:`HostProfile` already
satisfies - so a fully prepared host (every host check PASS) yields an empty
plan.

Two gates decide the plan:

1. **Doctor status.** The primary gate is the ``CheckResult.status``: an action
   is a candidate only for a host-scoped check whose status is
   :data:`~bakar.diagnostics.Status.FAIL`. A passing check needs no action. This
   matters because several actions (``DockerPullAction``,
   ``DockerStorageDriverAction``, ``DockerDaemonAction``) have an
   ``is_satisfied`` that returns ``False`` unconditionally - the live state is
   not on :class:`HostProfile` - so without the status gate a prepared host
   would never empty its plan.
2. **`is_satisfied(profile)`.** Of the candidates, any whose
   ``is_satisfied(profile)`` is ``True`` is dropped (the host already meets the
   target).

Only host-environment checks are mapped (host-tools, docker-daemon,
container-image, docker-ulimits, docker-storage-driver, sysctl,
git-global-config, cache-dirs); workspace/runtime checks (manifest, forks-*,
ti-*, bbsetup-*, kas-yaml-syntax, hashserv, bitbake-locks, bitbake-override,
sstate-hash-leak) are never mapped. The memory / disk-free /
workspace-filesystem / container-os / docker-version checks stay advisory: they
are reported as text, never turned into an applied action.

The docker *engine* install is advisory too: when docker is absent the plan
carries :func:`~bakar.setup.actions.tools.docker_engine_advice` text and no
docker-dependent action.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

import bakar.commands._app as _app
from bakar import config
from bakar.diagnostics import Status, run_all
from bakar.setup.actions.cache import CacheDirsAction
from bakar.setup.actions.config_write import ConfigWriteAction
from bakar.setup.actions.docker import (
    DockerDaemonAction,
    DockerGroupAction,
    DockerStorageDriverAction,
    DockerUlimitsAction,
)
from bakar.setup.actions.git import GitConfigAction
from bakar.setup.actions.sysctl import SysctlAction
from bakar.setup.actions.tools import (
    DockerPullAction,
    KasInstallAction,
    docker_engine_advice,
)

if TYPE_CHECKING:
    from bakar.config import BuildConfig
    from bakar.setup.actions.base import Action
    from bakar.setup.profile import HostProfile

# Host-environment checks ``setup`` remediates. A check NOT in this set is never
# mapped to an action - that excludes every workspace/runtime check (manifest,
# forks-*, ti-*, bbsetup-*, kas-yaml-syntax, hashserv, bitbake-locks,
# bitbake-override, sstate-hash-leak) and every advisory check (below).
_HOST_SCOPED_CHECKS: frozenset[str] = frozenset(
    {
        "host-tools",
        "docker-daemon",
        "container-image",
        "docker-ulimits",
        "docker-storage-driver",
        "sysctl",
        "git-global-config",
        "cache-dirs",
    }
)

# Checks reported as advisory text, never turned into an applied action. These
# are host-environment conditions setup surfaces but deliberately does not fix
# (a hardware fact or a host-policy decision the user must make).
_ADVISORY_CHECKS: frozenset[str] = frozenset(
    {
        "memory",
        "disk-free",
        "workspace-filesystem",
        "container-os",
        "docker-version",
    }
)

# Actions whose inclusion means setup applied a system knob whose value must be
# recorded in the global ``[host]`` config (so a follow-up doctor verifies
# against it). When any of these is in the plan, ``ConfigWriteAction`` is
# appended so the runner persists the applied values.
_VALUE_APPLYING_CHECKS: frozenset[str] = frozenset({"sysctl", "docker-ulimits"})


@dataclass(frozen=True)
class SetupPlan:
    """The computed remediation plan for one host.

    ``actions`` are the remediations to apply, in deterministic order, already
    filtered to FAILing host-scoped checks the host does not already satisfy.
    ``advisories`` are the reported-only conditions (advisory checks plus the
    docker-engine install hint) the runner prints but never acts on.
    """

    actions: list[Action] = field(default_factory=list)
    advisories: list[str] = field(default_factory=list)


def _candidate_actions(
    check_name: str,
    profile: HostProfile,
    cfg: BuildConfig,
    git_email: str | None,
    git_name: str | None,
) -> list[Action]:
    """Map one FAILing host-scoped ``check_name`` to its candidate action(s).

    Docker-dependent remediations are suppressed when docker is not installed -
    the docker-engine install is advisory (handled by the caller), so without
    the engine there is nothing for ``docker-daemon`` / ``container-image`` /
    ``docker-ulimits`` / ``docker-storage-driver`` to remediate.
    """
    if check_name == "host-tools":
        # Only the kas install is bakar-owned; the docker-engine part is
        # advisory (emitted separately by the caller).
        return [KasInstallAction()]
    if check_name == "sysctl":
        return [SysctlAction()]
    if check_name == "cache-dirs":
        return [CacheDirsAction()]
    if check_name == "git-global-config":
        # The identity comes from the command (CLI options or a prompt); without
        # it there is no value to write, so the action is skipped.
        if git_email is None or git_name is None:
            return []
        return [GitConfigAction(email=git_email, name=git_name)]

    # Remaining checks are docker-dependent. With no docker engine installed the
    # advisory install hint stands in their place and no action is produced.
    if not profile.docker_installed:
        return []
    if check_name == "docker-daemon":
        return [DockerDaemonAction(), DockerGroupAction()]
    if check_name == "container-image":
        return [DockerPullAction(image=cfg.container_image)]
    if check_name == "docker-ulimits":
        return [DockerUlimitsAction()]
    if check_name == "docker-storage-driver":
        return [DockerStorageDriverAction()]
    return []


def build(
    profile: HostProfile,
    *,
    cfg: BuildConfig | None = None,
    git_email: str | None = None,
    git_name: str | None = None,
) -> SetupPlan:
    """Build the :class:`SetupPlan` for ``profile``.

    ``cfg`` is resolved here when not supplied, mirroring ``commands/doctor.py``:
    ``config.resolve(workspace=Path.cwd(), user_config=bakar.commands._app._USER_CONFIG)``.
    The resolved ``container_image`` is passed into the docker-pull action - the
    action never calls ``resolve()`` itself.

    The git identity (``git_email`` / ``git_name``) is passed in by the command;
    when either is absent the ``git-global-config`` remediation is omitted.
    """
    if cfg is None:
        cfg = config.resolve(workspace=Path.cwd(), user_config=_app._USER_CONFIG)
        # setup prepares the container runtime, so it must always evaluate the
        # docker checks. resolve() auto-detects host_mode=True on a stock host
        # (no KAS_CONTAINER_IMAGE env, no configured image), and run_all then
        # filters out every _DOCKER_CHECKS member - which would silently drop
        # all docker remediations. Force host_mode off so they are assessed.
        cfg = replace(cfg, host_mode=False)

    results = run_all(cfg, None)

    actions: list[Action] = []
    advisories: list[str] = []
    has_value_applying = False

    for result in results:
        name = result.name
        if name in _ADVISORY_CHECKS:
            if result.status is Status.FAIL:
                advisories.append(f"{name}: {result.message}")
            continue
        if name not in _HOST_SCOPED_CHECKS:
            # Workspace/runtime check - never a setup action.
            continue
        if result.status is not Status.FAIL:
            # A passing host check needs no remediation.
            continue
        for action in _candidate_actions(name, profile, cfg, git_email, git_name):
            if action.is_satisfied(profile):
                continue
            actions.append(action)
            if name in _VALUE_APPLYING_CHECKS:
                has_value_applying = True

    # Record the docker-engine install advisory when docker is absent: the
    # engine install is never an applied action (bakar does not own the
    # cross-distro recipe).
    if not profile.docker_installed:
        advisories.append(docker_engine_advice(profile.pkg_manager))

    # When any value-applying action ran, persist the applied host knobs into
    # the global [host] config so a follow-up doctor verifies against them.
    if has_value_applying:
        persist = ConfigWriteAction()
        if not persist.is_satisfied(profile):
            actions.append(persist)

    return SetupPlan(actions=actions, advisories=advisories)
