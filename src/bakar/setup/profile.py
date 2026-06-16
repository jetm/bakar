"""Read-only host profiler for ``bakar setup``.

:func:`HostProfile.detect` reads the machine state the setup plan and the
per-action ``is_satisfied`` checks compare against: hardware capacity, the
distro package manager, docker availability, and the live values of the
sysctl/ulimit knobs setup may remediate. Detection NEVER mutates the host -
every read is a file read, a ``shutil``/``os`` query, or a group lookup; no
subprocess is spawned.
"""

from __future__ import annotations

import grp
import json
import os
import pwd
import shutil
from dataclasses import dataclass
from pathlib import Path

_OS_RELEASE = Path("/etc/os-release")
_MEMINFO = Path("/proc/meminfo")
_DAEMON_JSON = Path("/etc/docker/daemon.json")

# distro ``ID`` / ``ID_LIKE`` token -> package manager. The first token that
# matches wins; an unrecognised distro yields ``None`` (advisory-only install).
_PKG_MANAGER_BY_DISTRO: dict[str, str] = {
    "arch": "pacman",
    "debian": "apt",
    "ubuntu": "apt",
    "fedora": "dnf",
    "rhel": "dnf",
}


@dataclass(frozen=True)
class HostProfile:
    """A read-only snapshot of host state relevant to ``bakar setup``.

    The live sysctl/ulimit fields carry the values an Action's
    ``is_satisfied`` compares against its recommended target constants; a
    ``None`` field means the value was unreadable (treated as not-satisfied
    so the remediation still runs).
    """

    cpu_count: int
    mem_available_gb: float
    disk_free_gb: float
    distro_id: str | None
    pkg_manager: str | None
    in_docker_group: bool
    docker_installed: bool
    inotify_instances: int | None
    inotify_watches: int | None
    swappiness: int | None
    docker_nofile_soft: int | None

    @classmethod
    def detect(cls) -> HostProfile:
        """Build a :class:`HostProfile` from the live host (read-only)."""
        distro_id, pkg_manager = _detect_pkg_manager()
        return cls(
            cpu_count=os.cpu_count() or 1,
            mem_available_gb=_available_mem_gb(),
            disk_free_gb=_free_disk_gb(Path.home()),
            distro_id=distro_id,
            pkg_manager=pkg_manager,
            in_docker_group=_in_docker_group(),
            docker_installed=shutil.which("docker") is not None,
            inotify_instances=_read_sysctl("fs.inotify.max_user_instances"),
            inotify_watches=_read_sysctl("fs.inotify.max_user_watches"),
            swappiness=_read_sysctl("vm.swappiness"),
            docker_nofile_soft=_docker_nofile_soft(),
        )


def _parse_os_release(text: str) -> dict[str, str]:
    """Parse ``os-release`` ``KEY=value`` lines, stripping surrounding quotes."""
    parsed: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        parsed[key.strip()] = value.strip().strip('"').strip("'")
    return parsed


def _detect_pkg_manager() -> tuple[str | None, str | None]:
    """Map ``/etc/os-release`` ``ID``/``ID_LIKE`` to a package manager.

    ``ID`` is consulted first, then each whitespace-separated ``ID_LIKE``
    token. An unrecognised or missing distro yields ``(distro_id, None)``.
    """
    try:
        parsed = _parse_os_release(_OS_RELEASE.read_text())
    except OSError:
        return None, None
    distro_id = parsed.get("ID") or None
    candidates: list[str] = []
    if distro_id:
        candidates.append(distro_id)
    candidates.extend(parsed.get("ID_LIKE", "").split())
    for token in candidates:
        manager = _PKG_MANAGER_BY_DISTRO.get(token)
        if manager is not None:
            return distro_id, manager
    return distro_id, None


def _available_mem_gb() -> float:
    """Available RAM plus free swap in GB, read from ``/proc/meminfo``."""
    try:
        meminfo = _MEMINFO.read_text()
    except OSError:
        return 0.0
    free_kb = 0
    swap_kb = 0
    for line in meminfo.splitlines():
        if line.startswith("MemAvailable:"):
            free_kb = int(line.split()[1])
        elif line.startswith("SwapFree:"):
            swap_kb = int(line.split()[1])
    return (free_kb + swap_kb) / (1024**2)


def _free_disk_gb(path: Path) -> float:
    """Free space in GB on the filesystem backing ``path``."""
    try:
        return shutil.disk_usage(path).free / (1024**3)
    except OSError:
        return 0.0


def _in_docker_group() -> bool:
    """Whether the current user is a member of the ``docker`` group.

    Checks both the group's member list and the case where ``docker`` is the
    user's primary group. Returns ``False`` when no ``docker`` group exists.
    """
    try:
        docker_group = grp.getgrnam("docker")
    except KeyError:
        return False
    try:
        user = pwd.getpwuid(os.getuid())
    except KeyError:
        return False
    if user.pw_name in docker_group.gr_mem:
        return True
    return user.pw_gid == docker_group.gr_gid


def _read_sysctl(key: str) -> int | None:
    """Read an integer sysctl from ``/proc/sys`` (mirrors diagnostics)."""
    path = Path("/proc/sys") / key.replace(".", "/")
    try:
        return int(path.read_text().strip())
    except FileNotFoundError, ValueError:
        return None


def _docker_nofile_soft() -> int | None:
    """Live ``default-ulimits.nofile.Soft`` from ``/etc/docker/daemon.json``.

    Returns ``None`` when daemon.json is absent or unparseable; mirrors the
    read in :func:`bakar.diagnostics.check_docker_ulimits`.
    """
    try:
        data = json.loads(_DAEMON_JSON.read_text())
    except OSError, json.JSONDecodeError:
        return None
    nofile = data.get("default-ulimits", {}).get("nofile", {})
    soft = nofile.get("Soft")
    return soft if isinstance(soft, int) else None
