"""Privileged docker-daemon remediation actions for ``bakar setup``.

Three actions cover the docker checks ``setup`` automates:

- :class:`DockerUlimitsAction` (``docker-ulimits``) and
  :class:`DockerStorageDriverAction` (``docker-storage-driver``) both merge a
  single key into ``/etc/docker/daemon.json`` via a ``python3 -c`` round-trip
  that loads the existing JSON (or ``{}`` when absent), sets its key, validates
  the result parses, and writes after copying the original to
  ``daemon.json.bakar.bak``. A parse-validated round-trip cannot emit malformed
  JSON, and the load-merge-dump preserves every pre-existing key - a plain
  ``WriteFile`` of pre-rendered JSON could not. Never ``sed``/``echo``/``jq``.
- :class:`DockerDaemonAction` (``docker-daemon``) runs
  ``systemctl enable --now docker``.
- :class:`DockerGroupAction` (``docker-daemon``) runs
  ``usermod -aG docker $USER`` and warns - in both ``describe()`` and a printed
  notice operation - that the membership change needs a new login session.

Each action owns its recommended target as constants (it never reads
``resolve()``); ``is_satisfied`` compares the live value carried on the
:class:`HostProfile`.
"""

from __future__ import annotations

import getpass
from dataclasses import dataclass
from typing import TYPE_CHECKING

from bakar.setup.actions.base import RunCommand, WriteFile

if TYPE_CHECKING:
    from bakar.setup.profile import HostProfile

_DAEMON_JSON = "/etc/docker/daemon.json"
_DAEMON_JSON_BACKUP = "/etc/docker/daemon.json.bakar.bak"

# Recommended target constants. These exceed the doctor config floor
# (``host_nofile_soft`` default 8192); 65536 is the recommended value, so
# applying it satisfies the check.
NOFILE_SOFT = 65536
_NOFILE_HARD = 2097152
_STORAGE_DRIVER = "overlay2"


def _merge_command(key_path: list[str], value: object) -> RunCommand:
    """Build the ``python3 -c`` round-trip that merges one daemon.json key.

    The embedded script loads the existing JSON (``{}`` when the file is
    absent), backs the original up to ``daemon.json.bakar.bak`` when present and
    not already backed up (so a second merge in the same run cannot overwrite
    the original backup with an already-modified file), sets ``key_path`` to
    ``value`` (creating intermediate dicts), re-parses its
    own dump to prove the result is valid JSON, and only then writes it. The
    merge preserves every pre-existing top-level key because it mutates the
    loaded dict in place rather than rewriting the file from a template.
    """
    script = (
        "import json, os, shutil\n"
        f"p = {_DAEMON_JSON!r}\n"
        f"bak = {_DAEMON_JSON_BACKUP!r}\n"
        "data = {}\n"
        "if os.path.exists(p):\n"
        "    with open(p) as f:\n"
        "        data = json.load(f)\n"
        "    if not os.path.exists(bak):\n"
        "        shutil.copy2(p, bak)\n"
        f"keys = {key_path!r}\n"
        f"value = {value!r}\n"
        "node = data\n"
        "for k in keys[:-1]:\n"
        "    node = node.setdefault(k, {})\n"
        "node[keys[-1]] = value\n"
        "text = json.dumps(data, indent=2)\n"
        "json.loads(text)\n"
        "os.makedirs(os.path.dirname(p), exist_ok=True)\n"
        "with open(p, 'w') as f:\n"
        "    f.write(text + '\\n')\n"
    )
    return RunCommand(argv=["python3", "-c", script], needs_root=True)


@dataclass(frozen=True)
class DockerUlimitsAction:
    """Merge ``default-ulimits.nofile`` into ``/etc/docker/daemon.json``."""

    check_name: str = "docker-ulimits"
    needs_root: bool = True

    def describe(self) -> str:
        return (
            f"merge default-ulimits.nofile Soft={NOFILE_SOFT}/Hard={_NOFILE_HARD} "
            f"into {_DAEMON_JSON} (python3 round-trip, backs up {_DAEMON_JSON_BACKUP})"
        )

    def is_satisfied(self, profile: HostProfile) -> bool:
        soft = profile.docker_nofile_soft
        return soft is not None and soft >= NOFILE_SOFT

    def operations(self) -> list[RunCommand | WriteFile]:
        return [
            _merge_command(
                ["default-ulimits", "nofile"],
                {"Name": "nofile", "Soft": NOFILE_SOFT, "Hard": _NOFILE_HARD},
            ),
        ]


@dataclass(frozen=True)
class DockerStorageDriverAction:
    """Merge ``storage-driver: overlay2`` into ``/etc/docker/daemon.json``."""

    check_name: str = "docker-storage-driver"
    needs_root: bool = True

    def describe(self) -> str:
        return f"merge storage-driver={_STORAGE_DRIVER} into {_DAEMON_JSON} (python3 round-trip)"

    def is_satisfied(self, _profile: HostProfile) -> bool:
        # The live storage-driver is not carried on the profile; the merge is
        # idempotent (re-running sets the same key), so never skip it here.
        return False

    def operations(self) -> list[RunCommand | WriteFile]:
        return [_merge_command(["storage-driver"], _STORAGE_DRIVER)]


@dataclass(frozen=True)
class DockerDaemonAction:
    """Enable and start the docker daemon via ``systemctl``."""

    check_name: str = "docker-daemon"
    needs_root: bool = True

    def describe(self) -> str:
        return "systemctl enable --now docker"

    def is_satisfied(self, _profile: HostProfile) -> bool:
        return False

    def operations(self) -> list[RunCommand | WriteFile]:
        return [RunCommand(argv=["systemctl", "enable", "--now", "docker"], needs_root=True)]


@dataclass(frozen=True)
class DockerGroupAction:
    """Add the invoking user to the ``docker`` group.

    The membership change only takes effect in a new login session, so both
    :meth:`describe` and a printed notice operation warn about re-login.
    """

    username: str = ""
    check_name: str = "docker-daemon"
    needs_root: bool = True

    def _user(self) -> str:
        return self.username or getpass.getuser()

    def describe(self) -> str:
        return f"usermod -aG docker {self._user()} (requires a new login session to take effect)"

    def is_satisfied(self, profile: HostProfile) -> bool:
        return profile.in_docker_group

    def operations(self) -> list[RunCommand | WriteFile]:
        user = self._user()
        return [
            RunCommand(argv=["usermod", "-aG", "docker", user], needs_root=True),
            RunCommand(
                argv=[
                    "echo",
                    f"NOTE: '{user}' added to the docker group; log out and back in for it to take effect.",
                ],
                needs_root=True,
            ),
        ]
