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
from pathlib import Path
from typing import TYPE_CHECKING

from bakar.diagnostics import detect_buildtools, resolve_buildtools_dir
from bakar.setup.actions.base import RunCommand, WriteFile
from bakar.user_config import set_setting

if TYPE_CHECKING:
    from bakar.setup.profile import HostProfile

# Default host-level install dir for the buildtools-extended toolchain. The
# toolchain is host-wide (release-tied to the workspace's oe-core), so one
# install under $HOME serves every workspace.
DEFAULT_BUILDTOOLS_DIR = Path.home() / ".local" / "share" / "bakar" / "buildtools"

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
    ``config.resolve(...).kas_container_image`` once and passes it in; this action
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


class BuildtoolsInstallAction:
    """Install the ``buildtools-extended`` toolchain via oe-core's installer.

    Host-mode bitbake builds run against a pinned ``buildtools-extended``
    toolchain. This action runs the active workspace's
    ``openembedded-core/scripts/install-buildtools`` (whose default already
    installs the extended set) into a host-level dir, so the release matches the
    workspace's oe-core checkout. The script path is resolved by the plan
    builder and passed in; this action never resolves a workspace itself.

    Remediates the ``host-preflight`` check; unprivileged - the installer writes
    under ``$HOME`` and needs no root.
    """

    check_name = "host-preflight"
    needs_root = False

    def __init__(self, install_buildtools: str, install_dir: Path = DEFAULT_BUILDTOOLS_DIR) -> None:
        self.install_buildtools = install_buildtools
        self.install_dir = install_dir

    def describe(self) -> str:
        return f"install buildtools-extended (~63 MB) into {self.install_dir}"

    def is_satisfied(self, _profile: HostProfile) -> bool:
        """True when a buildtools toolchain is already resolvable.

        Reuses :func:`detect_buildtools` so there is one detector and one
        truth; the action self-drops when the toolchain is already present via
        the env var, config field, or an already-sourced sysroot.
        """
        return detect_buildtools().present

    def operations(self) -> list[RunCommand | WriteFile]:
        return [
            RunCommand(
                argv=[self.install_buildtools, "-d", str(self.install_dir)],
                needs_root=False,
            ),
        ]


class BuildtoolsConfigPersistAction:
    """Persist the buildtools install dir to the global ``[build]`` config.

    After :class:`BuildtoolsInstallAction` installs the toolchain, this records
    its location as ``[build] buildtools_dir`` so ``detect_buildtools`` resolves
    it in a fresh shell. Mirrors :class:`ConfigWriteAction`: the persist happens
    in :meth:`apply` (not via shell ``operations``), and the write goes to the
    user-global config (``set_setting`` default path), never a workspace
    ``.bakar.toml`` - host facts are machine-global.

    Carries the same ``host-preflight`` ``check_name`` as the install action so
    the plan drops it on a prepared host.
    """

    check_name = "host-preflight"
    needs_root = False

    def __init__(self, install_dir: Path = DEFAULT_BUILDTOOLS_DIR) -> None:
        self.install_dir = install_dir

    def describe(self) -> str:
        return f"record buildtools dir {self.install_dir} in the global [build] config"

    def is_satisfied(self, _profile: HostProfile) -> bool:
        """True when a buildtools toolchain is already resolvable.

        Defers to :func:`detect_buildtools` so a prepared host (toolchain
        already found) drops the persist along with the install.
        """
        return detect_buildtools().present

    def operations(self) -> list[RunCommand | WriteFile]:
        """No shell ops: the persist happens in :meth:`apply`, not via primitives."""
        return []

    def apply(self, path: Path | None = None) -> None:
        """Write ``build.buildtools_dir`` to the global config when the install took.

        Guards the persist by probing ``self.install_dir`` directly for an
        ``environment-setup-*`` script (via :func:`resolve_buildtools_dir`), not
        via :func:`detect_buildtools`: at apply time the env var and the config
        key being written are both unset and the install ran in a child process,
        so the env/config-driven detector cannot see the freshly installed dir.
        Probing the dir makes both install-failure modes safe without recording
        a dead config path:

        - a non-zero ``install-buildtools`` exit aborts the plan in the runner
          before this action runs, so the key is never written;
        - an install that exits 0 but drops no env-setup script leaves the probe
          ``present`` False here; this raises to surface the still-missing
          toolchain rather than silently reporting the host prepared (the spec
          requires a failed install to surface, not just to skip the write).

        ``path`` defaults to ``None``, routing ``set_setting`` to the user-global
        ``~/.config/bakar/config.toml``; tests pass an explicit path to assert
        the global-config target.
        """
        if not resolve_buildtools_dir(self.install_dir, "[build] buildtools_dir").present:
            raise RuntimeError(
                f"buildtools-extended install left no environment-setup-* script at "
                f"{self.install_dir}; the toolchain is still missing, so host builds "
                f"cannot proceed. Re-run the install or inspect the installer output."
            )
        set_setting("build.buildtools_dir", str(self.install_dir), path)
