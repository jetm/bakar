"""Shared cluster/cache doc + render helpers for monitor and the build UI.

``bakar monitor`` and the live ``bakar build`` Rich UI both show the
sccache-dist cluster line, the in-container sccache daemon's cache/dist line,
and (for ccache builds) a ccache hit/miss line. The doc helpers turn the
diagnostics report dataclasses into plain dicts (so the monitor JSON output is
stable) and the render helpers turn those dicts into Rich ``Text`` so both
surfaces share one rendering with no duplication.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from rich.text import Text

if TYPE_CHECKING:
    from bakar.diagnostics import BuildDaemonReport, CcacheReport, ClusterReport


def cluster_doc(report: ClusterReport, url: str | None) -> dict[str, Any]:
    cap = report.capacity
    return {
        "reachable": report.reachable,
        "scheduler_url": url,
        "error": report.error,
        "capacity": (
            {
                "num_servers": cap.num_servers,
                "num_cpus": cap.num_cpus,
                "in_progress": cap.in_progress,
                "servers": cap.servers,
            }
            if cap is not None
            else None
        ),
    }


def daemon_doc(report: BuildDaemonReport) -> dict[str, Any] | None:
    if not report.running:
        return None
    return {
        "container": report.container,
        "error": report.error,
        "cache_hits": report.cache_hits,
        "cache_misses": report.cache_misses,
        "distributed": report.distributed,
        "dist_errors": report.dist_errors,
        "cache_location": report.cache_location,
        "per_node": dict(report.per_node),
        "hits_by_lang": dict(report.cache_hits_by_lang),
        "misses_by_lang": dict(report.cache_misses_by_lang),
        "verdict": report.verdict,
    }


def ccache_doc(report: CcacheReport) -> dict[str, Any] | None:
    if not report.available:
        return None
    return {
        "cache_hits": report.cache_hits,
        "cache_misses": report.cache_misses,
        "hit_rate": report.hit_rate,
    }


def render_cluster(cluster: dict[str, Any]) -> list[Text]:
    """Render the cluster doc (shape from :func:`cluster_doc`) as Text lines.

    Returns the "cluster: ..." summary plus one dim per-node line each, or a
    single red "cluster: unreachable (...)" line when the scheduler is down.
    """
    parts: list[Text] = []
    cap = cluster["capacity"]
    if cluster["reachable"] and cap is not None:
        line = Text("cluster: ", style="bold")
        line.append(f"{cap['num_servers']} server(s), {cap['num_cpus']} cpus, {cap['in_progress']} job(s) in progress")
        parts.append(line)
        servers = cap["servers"]
        if servers:
            for node in servers:
                if isinstance(node, dict):
                    parts.append(
                        Text(
                            f"  {node.get('id', '?')} - {node.get('num_cpus', '?')} cpus, "
                            f"{node.get('in_progress', 0)} job(s)",
                            style="dim",
                        )
                    )
                else:
                    parts.append(Text(f"  {node}", style="dim"))
    else:
        parts.append(Text(f"cluster: unreachable ({cluster['error']})", style="red"))
    return parts


def render_sccache_cache(daemon: dict[str, Any] | None) -> Text:
    """Render the build-daemon doc (shape from :func:`daemon_doc`) as one Text."""
    if daemon is None:
        return Text("daemon: no build container running", style="dim")
    if daemon["error"]:
        return Text(f"daemon: stats unavailable ({daemon['error']})", style="yellow")
    colour = {"DISTRIBUTING": "green", "LOCAL-ONLY": "red"}.get(daemon["verdict"], "yellow")
    local = max(daemon["cache_misses"] - daemon["distributed"], 0)
    line = Text("daemon: ", style="bold")
    line.append(f"{daemon['verdict']}", style=colour)
    line.append(
        f"  cache {daemon['cache_hits']}/{daemon['cache_misses']} hit/miss  "
        f"dist {daemon['distributed']} (local {local}, errors {daemon['dist_errors']})"
    )
    hits_by_lang = daemon.get("hits_by_lang") or {}
    misses_by_lang = daemon.get("misses_by_lang") or {}
    for lang in sorted(set(hits_by_lang) | set(misses_by_lang)):
        hits = hits_by_lang.get(lang, 0)
        misses = misses_by_lang.get(lang, 0)
        total = hits + misses
        rate = (hits / total * 100) if total else 0.0
        line.append(f"\n  cache[{lang}]: {hits}/{misses} hit/miss ({rate:.0f}% hit)", style="dim")
    for node, jobs in (daemon.get("per_node") or {}).items():
        # Distribution is per-node and aggregated across all languages - sccache
        # exposes no per-language distribution, so this must not be read as
        # "language X distributed to node Y".
        line.append(f"\n  dist[{node}]: {jobs} job(s)", style="dim")
    return line


def render_ccache_cache(ccache: dict[str, Any] | None) -> Text:
    """Render the ccache doc (shape from :func:`ccache_doc`) as one Text."""
    if ccache is None:
        return Text("ccache: stats unavailable", style="dim")
    line = Text("ccache: ", style="bold")
    line.append(f"{ccache['cache_hits']}/{ccache['cache_misses']} hit/miss ({ccache['hit_rate']:.0f}% hit)")
    return line


def render_cluster_plain(cluster: dict[str, Any]) -> list[str]:
    """Plain-text sibling of :func:`render_cluster` (no markup/color, same fields)."""
    lines: list[str] = []
    cap = cluster["capacity"]
    if cluster["reachable"] and cap is not None:
        lines.append(
            f"cluster: {cap['num_servers']} server(s), {cap['num_cpus']} cpus, {cap['in_progress']} job(s) in progress"
        )
        for node in cap["servers"] or []:
            if isinstance(node, dict):
                lines.append(
                    f"  {node.get('id', '?')} - {node.get('num_cpus', '?')} cpus, {node.get('in_progress', 0)} job(s)"
                )
            else:
                lines.append(f"  {node}")
    else:
        lines.append(f"cluster: unreachable ({cluster['error']})")
    return lines


def render_sccache_cache_plain(daemon: dict[str, Any] | None) -> str:
    """Plain-text sibling of :func:`render_sccache_cache` (no markup/color, same fields)."""
    if daemon is None:
        return "daemon: no build container running"
    if daemon["error"]:
        return f"daemon: stats unavailable ({daemon['error']})"
    local = max(daemon["cache_misses"] - daemon["distributed"], 0)
    lines = [
        f"daemon: {daemon['verdict']}  "
        f"cache {daemon['cache_hits']}/{daemon['cache_misses']} hit/miss  "
        f"dist {daemon['distributed']} (local {local}, errors {daemon['dist_errors']})"
    ]
    hits_by_lang = daemon.get("hits_by_lang") or {}
    misses_by_lang = daemon.get("misses_by_lang") or {}
    for lang in sorted(set(hits_by_lang) | set(misses_by_lang)):
        hits = hits_by_lang.get(lang, 0)
        misses = misses_by_lang.get(lang, 0)
        total = hits + misses
        rate = (hits / total * 100) if total else 0.0
        lines.append(f"  cache[{lang}]: {hits}/{misses} hit/miss ({rate:.0f}% hit)")
    for node, jobs in (daemon.get("per_node") or {}).items():
        lines.append(f"  dist[{node}]: {jobs} job(s)")
    return "\n".join(lines)


def render_ccache_cache_plain(ccache: dict[str, Any] | None) -> str:
    """Plain-text sibling of :func:`render_ccache_cache` (no markup/color, same fields)."""
    if ccache is None:
        return "ccache: stats unavailable"
    return f"ccache: {ccache['cache_hits']}/{ccache['cache_misses']} hit/miss ({ccache['hit_rate']:.0f}% hit)"
