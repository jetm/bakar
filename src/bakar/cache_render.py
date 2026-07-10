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


def _daemon_local_count(daemon: dict[str, Any]) -> int:
    """Locally-compiled (non-distributed) count, clamped to 0.

    sccache's ``cache_misses`` counter includes both locally-compiled and
    distributed misses; subtracting ``distributed`` isolates the local-only
    portion. Clamped because a mid-build counter reset can otherwise produce
    a transient negative value.
    """
    return max(daemon["cache_misses"] - daemon["distributed"], 0)


def _daemon_lang_rates(daemon: dict[str, Any]) -> list[tuple[str, int, int, float]]:
    """Per-language (lang, hits, misses, hit_rate_pct) rows, sorted by lang name."""
    hits_by_lang = daemon.get("hits_by_lang") or {}
    misses_by_lang = daemon.get("misses_by_lang") or {}
    rows: list[tuple[str, int, int, float]] = []
    for lang in sorted(set(hits_by_lang) | set(misses_by_lang)):
        hits = hits_by_lang.get(lang, 0)
        misses = misses_by_lang.get(lang, 0)
        rows.append((lang, hits, misses, cache_hit_pct(hits, misses)))
    return rows


def render_sccache_cache(daemon: dict[str, Any] | None) -> Text:
    """Render the build-daemon doc (shape from :func:`daemon_doc`) as one Text."""
    if daemon is None:
        return Text("daemon: no build container running", style="dim")
    if daemon["error"]:
        return Text(f"daemon: stats unavailable ({daemon['error']})", style="yellow")
    colour = {"DISTRIBUTING": "green", "LOCAL-ONLY": "red"}.get(daemon["verdict"], "yellow")
    local = _daemon_local_count(daemon)
    line = Text("daemon: ", style="bold")
    line.append(f"{daemon['verdict']}", style=colour)
    line.append(
        f"  cache {daemon['cache_hits']}/{daemon['cache_misses']} hit/miss  "
        f"dist {daemon['distributed']} (local {local}, errors {daemon['dist_errors']})"
    )
    for lang, hits, misses, rate in _daemon_lang_rates(daemon):
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
    local = _daemon_local_count(daemon)
    lines = [
        f"daemon: {daemon['verdict']}  "
        f"cache {daemon['cache_hits']}/{daemon['cache_misses']} hit/miss  "
        f"dist {daemon['distributed']} (local {local}, errors {daemon['dist_errors']})"
    ]
    for lang, hits, misses, rate in _daemon_lang_rates(daemon):
        lines.append(f"  cache[{lang}]: {hits}/{misses} hit/miss ({rate:.0f}% hit)")
    for node, jobs in (daemon.get("per_node") or {}).items():
        lines.append(f"  dist[{node}]: {jobs} job(s)")
    return "\n".join(lines)


def render_ccache_cache_plain(ccache: dict[str, Any] | None) -> str:
    """Plain-text sibling of :func:`render_ccache_cache` (no markup/color, same fields)."""
    if ccache is None:
        return "ccache: stats unavailable"
    return f"ccache: {ccache['cache_hits']}/{ccache['cache_misses']} hit/miss ({ccache['hit_rate']:.0f}% hit)"


# Build-end summary + live-badge helpers ---------------------------------
#
# The build runner persists per-build cache deltas (``cache_delta``) and prints
# a greppable ``bakar[cache]`` summary at build end, plus drives live at-a-glance
# badges in the build UI. These helpers keep all cache rendering in this module.

_CACHE_SUMMARY_PREFIX = "bakar[cache]"

# Verdict -> compact dist state used by both the plain token and the Rich badge.
_DIST_STATE = {"DISTRIBUTING": "on", "LOCAL-ONLY": "off"}
_DIST_COLOUR = {"on": "green", "off": "red", "idle": "yellow"}


def cache_hit_pct(hits: int, misses: int) -> float:
    """Hit-rate percentage from raw hit/miss counts, guarding divide-by-zero.

    The sccache ``daemon_doc`` carries no aggregate ``hit_rate``, only counts;
    this recomputes it and returns ``0.0`` when both counters are zero rather
    than raising ``ZeroDivisionError``.
    """
    total = hits + misses
    return (100.0 * hits / total) if total else 0.0


def cache_badge_token(hit_pct: float) -> str:
    """Pure-``str`` ``cache=NN%`` live-badge token (no markup/ANSI)."""
    return f"cache={hit_pct:.0f}%"


def dist_badge_token(verdict: str | None) -> str:
    """Pure-``str`` ``dist=on|off|idle`` token from a daemon verdict."""
    return f"dist={_DIST_STATE.get(verdict or '', 'idle')}"


def cache_badge_rich(hit_pct: float) -> Text:
    """Rich cache badge: a Nerd-Font database glyph plus the hit-rate percent."""
    badge = Text(" ", style="cyan")  # nf-fa-database
    badge.append(f"{hit_pct:.0f}%")
    return badge


def dist_badge_rich(verdict: str | None) -> Text:
    """Rich dist badge: a Nerd-Font sitemap glyph coloured by the verdict."""
    state = _DIST_STATE.get(verdict or "", "idle")
    colour = _DIST_COLOUR[state]
    badge = Text(" ", style=colour)  # nf-fa-sitemap
    badge.append(f"dist {state}", style=colour)
    return badge


# Cache-backend badge glyphs. The sitemap/database glyphs match the Nerd-Font
# glyphs named by dist_badge_rich/cache_badge_rich; the ban glyph is new to the
# "none" state. The three are distinct so each classified state reads apart at a
# glance.
_BACKEND_SITEMAP_GLYPH = ""  # nf-fa-sitemap (sccache)
_BACKEND_DATABASE_GLYPH = ""  # nf-fa-database (ccache)
_BACKEND_BAN_GLYPH = ""  # nf-fa-ban (none)

# Classified backend -> (glyph, colour). Unclassified (``None``) is deliberately
# absent so it falls through to an empty glyph, distinct from "none"'s ban badge.
_CACHE_BACKEND_BADGE = {
    "sccache": (_BACKEND_SITEMAP_GLYPH, "green"),
    "ccache": (_BACKEND_DATABASE_GLYPH, "cyan"),
    "none": (_BACKEND_BAN_GLYPH, "dim red"),
}


def cache_backend_badge(backend: str | None) -> tuple[str, str]:
    """``(glyph, colour)`` for a classified cache backend.

    Each of the three classified states gets a distinct glyph/colour. An
    unclassified backend (``None``) returns ``("", "")`` - an empty glyph, which
    is distinct from the visible ban badge of the ``"none"`` classified state.
    """
    return _CACHE_BACKEND_BADGE.get(backend or "", ("", ""))


def cache_delta(first_doc: dict[str, Any] | None, last_doc: dict[str, Any] | None) -> dict[str, Any] | None:
    """Per-build cache delta between the first-probe baseline and the last doc.

    Subtracts the baseline counts from the last counts, recomputes ``hit_rate``
    from the delta, clamps any negative count (a mid-build counter reset) to 0,
    and tags ``window``. Returns the cumulative ``last_doc`` tagged
    ``window="lifetime"`` when ``first_doc`` is ``None`` (no baseline was
    captured); otherwise tags ``window="build"``. Returns ``None`` when there is
    no last doc to summarize.
    """
    if not last_doc:
        return None
    if first_doc is None:
        doc = dict(last_doc)
        doc["window"] = "lifetime"
        return doc
    delta = dict(last_doc)
    for key in ("cache_hits", "cache_misses", "distributed", "dist_errors"):
        if key in last_doc:
            delta[key] = max(int(last_doc.get(key, 0)) - int(first_doc.get(key, 0)), 0)
    for lang_key in ("hits_by_lang", "misses_by_lang"):
        if lang_key in last_doc:
            base = first_doc.get(lang_key) or {}
            delta[lang_key] = {k: max(v - base.get(k, 0), 0) for k, v in last_doc[lang_key].items()}
    if "per_node" in last_doc:
        base = first_doc.get("per_node") or {}
        delta["per_node"] = {k: max(v - base.get(k, 0), 0) for k, v in last_doc["per_node"].items()}
    if "hit_rate" in last_doc:
        delta["hit_rate"] = cache_hit_pct(delta.get("cache_hits", 0), delta.get("cache_misses", 0))
    delta["window"] = "build"
    return delta


def build_end_summary_plain(doc: dict[str, Any] | None, backend: str) -> str:
    """Plain, ANSI-free build-end cache summary tagged for CI discrimination.

    Prepends the stable ``bakar[cache]`` prefix and appends ``backend=`` and
    ``window=`` tokens on the summary line. Returns ``""`` when no backend was
    active (``doc`` is ``None``).
    """
    if not doc:
        return ""
    window = doc.get("window", "build")
    body = render_sccache_cache_plain(doc) if backend == "sccache" else render_ccache_cache_plain(doc)
    lines = body.split("\n")
    lines[0] = f"{_CACHE_SUMMARY_PREFIX} {lines[0]}  backend={backend} window={window}"
    return "\n".join(lines)


def build_end_summary_rich(doc: dict[str, Any] | None, backend: str) -> Text:
    """Rich build-end cache summary (styled ``Text``, unprefixed)."""
    return render_sccache_cache(doc) if backend == "sccache" else render_ccache_cache(doc)
