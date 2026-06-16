"""Tool-install and image-pull actions for ``bakar setup``.

This module owns the two bakar-managed remediations that are NOT OS packages:
installing ``kas`` via ``uv tool install`` (:class:`KasInstallAction`) and
pulling the build container image (:class:`DockerPullAction`). Both are
unprivileged - they run in the user context, never under the single ``sudo``
script.

The docker *engine* itself is advisory only: bakar does not own the
cross-distro Docker CE install recipe (GPG keys, repos, package names). When
docker is absent, :func:`docker_engine_advice` returns the official per-distro
install command as text for the plan to print; it contributes NO applied
action and therefore no docker-dependent action either.
"""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING

from bakar.setup.actions.base import RunCommand, WriteFile

if TYPE_CHECKING:
    from bakar.setup.profile import HostProfile

# Official per-distro docker-engine install commands (docs.docker.com). Used as
# advisory text only - bakar never runs these.
_DOCKER_ENGINE_INSTALL: dict[str, str] = {
    "pacman": "sudo pacman -S docker",
    "apt": "sudo apt-get install docker-ce docker-ce-cli containerd.io",
    "dnf": "sudo dnf install docker-ce docker-ce-cli containerd.io",
}
_DOCKER_ENGINE_INSTALL_URL = "https://docs.docker.com/engine/install/"


class KasInstallAction:
    """Install ``kas`` via ``uv tool install`` (never a distro package).

    ``kas`` is bakar-owned tooling, not an OS package, so it always installs
    through ``uv tool install`` regardless of the host distro. Remediates the
    ``host-tools`` check; unprivileged.
    """

    check_name = "host-tools"
    needs_root = False

    def describe(self) -> str:
        return "install kas via `uv tool install kas`"

    def is_satisfied(self, _profile: HostProfile) -> bool:
        """True when the ``kas`` binary is already on PATH."""
        return shutil.which("kas") is not None

    def operations(self) -> list[RunCommand | WriteFile]:
        return [RunCommand(argv=["uv", "tool", "install", "kas"], needs_root=False)]


class DockerPullAction:
    """Pull the build container image into the local docker store.

    The image string is supplied by the plan builder, which resolves
    ``config.resolve(...).container_image`` once and passes it in; this action
    never calls ``resolve()`` itself. Remediates the ``container-image`` check;
    unprivileged (the user needs only docker-group membership, not root).
    """

    check_name = "container-image"
    needs_root = False

    def __init__(self, image: str) -> None:
        self.image = image

    def describe(self) -> str:
        return f"pull container image `{self.image}` via docker pull"

    def is_satisfied(self, _profile: HostProfile) -> bool:
        """Whether the image is already present locally.

        :class:`HostProfile` carries no per-image presence field, so this
        action cannot self-determine satisfaction; the plan builder drops it
        when the ``container-image`` doctor check already PASSes. Returning
        ``False`` here means "pull unless the plan decides otherwise".
        """
        return False

    def operations(self) -> list[RunCommand | WriteFile]:
        return [RunCommand(argv=["docker", "pull", self.image], needs_root=False)]


def docker_engine_advice(pkg_manager: str | None) -> str:
    """Advisory install command/URL for the docker engine on this distro.

    Returns the official per-distro install command when ``pkg_manager`` is one
    of the recognised managers, otherwise a pointer to the upstream install
    docs for an unknown distro. This is advisory TEXT only - it is never wrapped
    in an :class:`Action` and never executed, because bakar does not own the
    cross-distro Docker CE recipe.
    """
    command = _DOCKER_ENGINE_INSTALL.get(pkg_manager or "")
    if command is None:
        return f"Install Docker Engine for your distro: {_DOCKER_ENGINE_INSTALL_URL}"
    return f"Install Docker Engine: {command}"
