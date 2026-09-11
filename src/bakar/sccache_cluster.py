"""Pure sccache-dist cluster computation: server discovery, host-list parsing.

Extracted from ``commands/clean_cache.py`` (855 lines, 4.8x the median module
size). These helpers do the arithmetic and text-parsing side of the
``--full`` cold-reset: resolving which secondary build servers exist, parsing
``sccache --dist-status`` output, and building the shell command strings the
command layer runs over ssh/subprocess. They import no ``bakar.commands`` or
``bakar.fsremove`` symbol, so they sit in the foundation layer; the printing
(``console.print``) and prompting (``typer.confirm``/``typer.Exit``) wrappers
around them stay in ``commands/clean_cache.py``, which is the layer allowed to
reach those effects.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import time
from pathlib import Path

# ``sccache --dist-status`` routes through the local client daemon, which the
# first call auto-starts, so it is retried until the scheduler reports its
# ``servers``.
_DIST_STATUS_RETRIES = 3


def _local_ips() -> set[str]:
    """Return this host's interface IPs via ``ip -o addr show`` (empty on failure).

    Column 4 of each ``ip -o addr`` row is ``<addr>/<prefix>``; the prefix is
    stripped so a server ``id`` can be matched against the bare address.
    """
    try:
        out = subprocess.run(
            ["ip", "-o", "addr", "show"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except FileNotFoundError, subprocess.TimeoutExpired:
        return set()
    ips: set[str] = set()
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 4:
            ips.add(parts[3].split("/", 1)[0])
    return ips


def _parse_dist_status_servers(status_json: str, local_ips: set[str]) -> list[str]:
    """Return the secondary server hosts named in a ``sccache --dist-status`` blob.

    Mirrors the bash python filter: reads ``SchedulerStatus[1].servers[].id``,
    strips the ``:port`` suffix, drops this host's own IPs, and dedupes while
    preserving order. Returns [] on malformed JSON or an unexpected shape.
    """
    try:
        status = json.loads(status_json)
    except ValueError, TypeError:
        return []
    sched = status.get("SchedulerStatus") if isinstance(status, dict) else None
    if not isinstance(sched, list) or len(sched) < 2 or not isinstance(sched[1], dict):
        return []
    servers = sched[1].get("servers") or []
    hosts: list[str] = []
    seen: set[str] = set()
    for srv in servers:
        if not isinstance(srv, dict):
            continue
        host = (srv.get("id") or "").rsplit(":", 1)[0]
        if host and host not in local_ips and host not in seen:
            seen.add(host)
            hosts.append(host)
    return hosts


def _resolve_secondaries() -> list[str]:
    """Resolve the secondary (non-local) sccache-dist build servers to reset.

    Precedence mirrors clean-all-cache.sh: an explicit ``SECONDARY_NODES`` env
    override (space-split) wins; otherwise the live server list reported by
    ``sccache --dist-status`` with this host's own IPs filtered out. ``--dist-status``
    routes through the local client daemon, which the first call auto-starts, so it
    is retried until the scheduler reports its ``servers``. Returns [] when sccache
    is absent or the scheduler never reports its servers.
    """
    override = os.environ.get("SECONDARY_NODES")
    if override:
        return override.split()
    if shutil.which("sccache") is None:
        return []
    status_json = ""
    for attempt in range(_DIST_STATUS_RETRIES):
        try:
            out = subprocess.run(
                ["sccache", "--dist-status"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except FileNotFoundError, subprocess.TimeoutExpired:
            return []
        status_json = out.stdout
        if '"servers"' in status_json:
            break
        if attempt < _DIST_STATUS_RETRIES - 1:
            time.sleep(1)
    if not status_json:
        return []
    return _parse_dist_status_servers(status_json, _local_ips())


def _sccache_client_cache_dirs() -> list[Path]:
    """Return the sccache client disk-cache dirs under ``~/.cache`` to wipe."""
    cache = Path.home() / ".cache"
    dirs = [cache / "sccache", cache / "sccache-dist-client"]
    dirs.extend(sorted(cache.glob("sccache-dist-client.stale.*")))
    return dirs


def _reset_dist_server_cmd() -> str:
    """Return the shell command that wipes and reinitialises the sccache-dist server.

    The build server does a NON-recursive ``mkdir`` of ``build/toolchains/<hash>``
    per job, so ``build/toolchains`` must exist or every distributed compile fails
    with "failed to prepare overlay dirs" (HTTP 500). Recreate that subdir
    explicitly and restart the service so its in-memory toolchain refs match the
    wiped disk.
    """
    return (
        "sudo rm -rf /var/cache/sccache-dist/toolchains /var/cache/sccache-dist/build "
        "&& sudo mkdir -p /var/cache/sccache-dist/toolchains /var/cache/sccache-dist/build/toolchains "
        "&& sudo systemctl restart sccache-server"
    )


def _remote_reset_cmd(build_dirs: list[Path], reset_cmd: str) -> str:
    """Build the ssh command run on a secondary: wipe its build dir(s), then reset.

    The secondary runs its own bitbake into a per-node (not shared) build dir, so a
    cold multi-node run must clear it too. The shared SSTATE_DIR is NOT re-wiped
    remotely - the local in-place empty already cooled the single NFS copy. Each
    build dir path is shell-quoted: an unquoted path containing spaces or shell
    metacharacters would otherwise break or reinterpret the remote command.
    """
    prefix = "".join(f"rm -rf {shlex.quote(str(b))}; " for b in build_dirs)
    return prefix + reset_cmd
