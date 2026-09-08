"""Live cluster, build-daemon and ccache probes.

Each probe answers a question about a *running* system - the dist scheduler's
capacity, the sccache daemon's stats, the host ccache's hit rate - rather than
gating a build the way a ``bakar.diagnostics`` check does. Never raises: every
probe returns a report carrying the reason it could not answer.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from bakar import build_stop

if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class ClusterCapacity:
    """Aggregate scheduler capacity from ``sccache --dist-status``.

    ``servers`` is None against the current upstream scheduler, which serializes
    only the aggregate counts. It is parsed opportunistically so a forked
    scheduler that adds a per-server array lights up the node table without a
    bakar-side change.
    """

    num_servers: int
    num_cpus: int
    in_progress: int
    servers: list | None = None


@dataclass
class ClusterReport:
    """Result of probing the dist scheduler.

    ``reachable`` is True only when ``sccache --dist-status`` returned parseable
    capacity; ``error`` carries a short human reason otherwise.
    """

    reachable: bool
    capacity: ClusterCapacity | None = None
    error: str | None = None


def _parse_cluster_status(stdout: str) -> ClusterCapacity | None:
    """Parse `sccache --dist-status` JSON into a :class:`ClusterCapacity`.

    Returns None on any parse failure or unexpected shape - cluster status is
    informational and must never raise into a caller's gate.
    """
    try:
        info = json.loads(stdout)["SchedulerStatus"][1]
        return ClusterCapacity(
            num_servers=info["num_servers"],
            num_cpus=info["num_cpus"],
            in_progress=info["in_progress"],
            servers=info.get("servers"),
        )
    except ValueError, KeyError, IndexError, TypeError:
        return None


def _format_capacity(cap: ClusterCapacity) -> str:
    """Render a :class:`ClusterCapacity` as the one-line preflight summary."""
    return f"{cap.num_servers} build server(s), {cap.num_cpus} cpus, {cap.in_progress} job(s) in progress"


def _parse_cluster_capacity(stdout: str) -> str | None:
    """Summarize `sccache --dist-status` JSON for the preflight message.

    Returns a string like "2 build server(s), 64 cpus, 0 job(s) in progress" so
    the user sees the live cluster size before the build, or None on any parse
    failure - the capacity line is informational and must never fail the gate.
    """
    cap = _parse_cluster_status(stdout)
    return None if cap is None else _format_capacity(cap)


def probe_cluster(scheduler_url: str | None = None) -> ClusterReport:
    """Query the dist scheduler via ``sccache --dist-status`` and report capacity.

    When ``scheduler_url`` is given it is forwarded as ``SCCACHE_DIST_SCHEDULER_URL``
    so the probe targets that cluster instead of the one in sccache's own config.
    Never raises: a missing binary, a failed subprocess, or an unparseable
    response all return an unreachable :class:`ClusterReport` carrying the reason.
    """
    env = None
    if scheduler_url:
        env = {**os.environ, "SCCACHE_DIST_SCHEDULER_URL": scheduler_url}
    try:
        status = subprocess.run(
            ["sccache", "--dist-status"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env=env,
        )
    except FileNotFoundError:
        return ClusterReport(reachable=False, error="sccache binary not found on PATH")
    except (OSError, subprocess.SubprocessError) as exc:
        return ClusterReport(reachable=False, error=f"sccache --dist-status failed: {exc}")
    if status.returncode != 0:
        detail = status.stderr.strip() or status.stdout.strip()
        msg = f"sccache --dist-status exited {status.returncode}"
        return ClusterReport(reachable=False, error=f"{msg}: {detail}" if detail else msg)
    cap = _parse_cluster_status(status.stdout)
    if cap is None:
        detail = status.stderr.strip()
        base = "scheduler unreachable or returned no capacity"
        return ClusterReport(reachable=False, error=f"{base}: {detail}" if detail else base)
    return ClusterReport(reachable=True, capacity=cap)


@dataclass
class BuildDaemonReport:
    """In-container sccache daemon view for a running bakar build.

    ``running`` is False when no build container is up. ``distributed`` is the
    total jobs sent to the cluster; ``per_node`` breaks it down by server. A
    build that compiles (``cache_misses`` > 0) with ``distributed`` == 0 is the
    local-only failure mode the dist guard exists to catch.
    """

    running: bool
    container: str | None = None
    error: str | None = None
    cache_hits: int = 0
    cache_misses: int = 0
    cache_hits_by_lang: dict[str, int] = field(default_factory=dict)
    cache_misses_by_lang: dict[str, int] = field(default_factory=dict)
    distributed: int = 0
    dist_errors: int = 0
    cache_location: str | None = None
    per_node: tuple[tuple[str, int], ...] = ()

    @property
    def verdict(self) -> str:
        if not self.running:
            return "no build container running"
        if self.error:
            return "stats unavailable"
        if self.distributed > 0:
            return "DISTRIBUTING"
        if self.cache_misses > 0:
            return "LOCAL-ONLY"
        return "idle (no compiles yet)"


def probe_build_daemon() -> BuildDaemonReport:
    """Inspect the sccache daemon inside a running bakar build container.

    Finds the build container by its ``bakar.run_id`` label and queries the
    in-container daemon's stats, so ``bakar cluster-info`` can show whether an
    in-progress build is actually distributing - not just the scheduler's
    aggregate capacity, which says nothing about the client. Uses
    :func:`bakar.build_stop.detect_runtime` to resolve the container runtime
    (docker or podman) the same way ``kas-container`` does. Host-mode builds
    run no container, so an empty result falls back to the host UDS
    daemon (:func:`_probe_host_uds_daemon`). Never raises: returns
    ``running=False`` only when neither a container nor a host daemon answers,
    and ``error=...`` when the query fails.
    """
    runtime = build_stop.detect_runtime()
    try:
        ps = subprocess.run(
            [runtime, "ps", "--filter", "label=bakar.run_id", "--format", "{{.ID}}"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError) as exc:
        return BuildDaemonReport(running=False, error=f"{runtime} ps failed: {exc}")
    cids = ps.stdout.split()
    if not cids:
        # No build container (host-mode build): fall back to the host UDS daemon.
        return _probe_host_uds_daemon()
    cid = cids[0]
    return _query_sccache_daemon([runtime, "exec", cid, "sccache"], env=None, cid=cid)


def _query_sccache_daemon(sccache_argv: list[str], env: dict[str, str] | None, cid: str | None) -> BuildDaemonReport:
    """Query a running sccache daemon's stats + cache location and map to a report.

    ``sccache_argv`` is the command prefix that reaches the daemon: ``docker exec
    <cid> sccache`` for the in-container probe, or ``sccache`` with ``env`` carrying
    ``SCCACHE_SERVER_UDS`` for the host UDS probe. Returns an error report when the
    JSON stats query fails; the ``Cache location`` scan is best-effort.
    """
    try:
        out = subprocess.run(
            [*sccache_argv, "--show-stats", "--stats-format=json"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            env=env,
        )
        stats = json.loads(out.stdout)["stats"]
    except (OSError, subprocess.SubprocessError, ValueError, KeyError) as exc:
        return BuildDaemonReport(running=True, container=cid, error=f"stats query failed: {exc}")
    location = None
    try:
        txt = subprocess.run(
            [*sccache_argv, "--show-stats"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            env=env,
        )
        for line in txt.stdout.splitlines():
            if line.strip().startswith("Cache location"):
                location = line.split("Cache location", 1)[1].strip()
                break
    except OSError, subprocess.SubprocessError:
        pass
    return _build_daemon_report_from_stats(stats, cid, location)


def _probe_host_uds_daemon() -> BuildDaemonReport:
    """Query the host-mode sccache daemon over its unix-domain socket.

    Host-mode builds run no ``bakar.run_id`` container, so
    :func:`probe_build_daemon`'s docker path finds nothing; the client daemon
    still answers on the host UDS (:func:`bakar.sccache_server.default_uds_path`).
    Query it directly so per-language and per-node stats surface for host builds
    too. Returns ``running=False`` when no host daemon answers, and never
    auto-starts one - the ``_uds_responding`` pre-check avoids sccache's implicit
    server spawn on ``--show-stats``.
    """
    from bakar import sccache_server

    uds = str(sccache_server.default_uds_path())
    if not sccache_server._uds_responding(uds):
        return BuildDaemonReport(running=False)
    env = {**os.environ, "SCCACHE_SERVER_UDS": uds}
    return _query_sccache_daemon(["sccache"], env=env, cid=None)


def _build_daemon_report_from_stats(stats: dict, cid: str | None, location: str | None) -> BuildDaemonReport:
    """Map an sccache ``--show-stats --stats-format=json`` ``stats`` block to a report.

    Pure (no docker/subprocess) so it is unit-testable without a build
    container. Preserves the per-language ``counts`` dicts sccache keys by
    display name (``C/C++``, ``Rust``, ``Assembler``) and sets the scalar
    ``cache_hits``/``cache_misses`` totals to the sums of those dicts, keeping
    the existing aggregate contract every scalar caller relies on. Missing or
    empty ``counts`` yield empty dicts and zero totals without raising.
    """
    dist = stats.get("dist_compiles", {}) or {}
    hits_by_lang = dict(stats.get("cache_hits", {}).get("counts", {}))
    misses_by_lang = dict(stats.get("cache_misses", {}).get("counts", {}))
    return BuildDaemonReport(
        running=True,
        container=cid,
        cache_hits=sum(hits_by_lang.values()),
        cache_misses=sum(misses_by_lang.values()),
        cache_hits_by_lang=hits_by_lang,
        cache_misses_by_lang=misses_by_lang,
        distributed=sum(dist.values()),
        dist_errors=int(stats.get("dist_errors", 0) or 0),
        cache_location=location,
        per_node=tuple(sorted(dist.items())),
    )


@dataclass
class CcacheReport:
    """Host ccache hit/miss view for a running bakar build.

    ``available`` is False when the cache dir is absent, the ``ccache`` binary
    is missing, or ``ccache --print-stats`` fails; ``error`` then names why.
    On success ``cache_hits`` sums the direct and preprocessed hits and
    ``cache_misses`` is the miss count.
    """

    available: bool
    cache_hits: int = 0
    cache_misses: int = 0
    error: str | None = None

    @property
    def hit_rate(self) -> float:
        total = self.cache_hits + self.cache_misses
        return 100.0 * self.cache_hits / total if total else 0.0


def probe_ccache(ccache_dir: Path) -> CcacheReport:
    """Read host ccache hit/miss counts from ``ccache --print-stats``.

    Mirrors the ``ccache-health`` doctor check's guards: returns
    ``available=False`` when the cache dir is absent, the ``ccache`` binary is
    missing, or the stats command fails/times out. Never raises. On success
    sums ``cache_hit_direct`` + ``cache_hit_preprocessed`` into ``cache_hits``
    and reads ``cache_miss`` into ``cache_misses``.
    """
    if not ccache_dir.exists():
        return CcacheReport(available=False, error=f"{ccache_dir} absent")

    if shutil.which("ccache") is None:
        return CcacheReport(available=False, error="ccache binary not on PATH")

    env = {**os.environ, "CCACHE_DIR": str(ccache_dir)}
    try:
        out = subprocess.run(
            ["ccache", "--print-stats"],
            capture_output=True,
            text=True,
            timeout=5,
            env=env,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return CcacheReport(available=False, error=f"ccache --print-stats failed: {exc}")
    if out.returncode != 0:
        return CcacheReport(
            available=False,
            error=out.stderr.strip() or "ccache --print-stats exited non-zero",
        )

    hit_direct = 0
    hit_preprocessed = 0
    misses = 0
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        key, value = parts
        try:
            count = int(value)
        except ValueError:
            continue
        if key == "cache_hit_direct":
            hit_direct = count
        elif key == "cache_hit_preprocessed":
            hit_preprocessed = count
        elif key == "cache_miss":
            misses = count

    return CcacheReport(available=True, cache_hits=hit_direct + hit_preprocessed, cache_misses=misses)


def _query_cluster_capacity() -> str | None:
    """Run `sccache --dist-status` and return its capacity summary, or None.

    Used by the container path, which has no host-side reachability probe of its
    own; the scheduler's capacity is global, so the host can still report it.
    """
    report = probe_cluster()
    return None if report.capacity is None else _format_capacity(report.capacity)
