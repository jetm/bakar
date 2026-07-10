"""Unit tests for ``bakar.cache_render``.

The render helpers turn plain dicts (the shape the doc helpers emit) into Rich
``Text``; assertions read ``.plain`` so they ignore styling. No subprocess or
cluster access is involved.
"""

from __future__ import annotations

import pytest
from rich.text import Text

from bakar.cache_render import (
    _BACKEND_BAN_GLYPH,
    _BACKEND_DATABASE_GLYPH,
    _BACKEND_SITEMAP_GLYPH,
    cache_backend_badge,
    daemon_doc,
    render_ccache_cache,
    render_cluster,
    render_sccache_cache,
)
from bakar.diagnostics import BuildDaemonReport

pytestmark = pytest.mark.unit


def test_render_cluster_reachable_with_two_nodes() -> None:
    cluster = {
        "reachable": True,
        "scheduler_url": "http://sched:10600",
        "error": None,
        "capacity": {
            "num_servers": 2,
            "num_cpus": 64,
            "in_progress": 3,
            "servers": [
                {"id": "node-a", "num_cpus": 32, "in_progress": 2},
                {"id": "node-b", "num_cpus": 32, "in_progress": 1},
            ],
        },
    }
    lines = render_cluster(cluster)
    assert len(lines) == 3
    assert lines[0].plain == "cluster: 2 server(s), 64 cpus, 3 job(s) in progress"
    assert lines[1].plain == "  node-a - 32 cpus, 2 job(s)"
    assert lines[2].plain == "  node-b - 32 cpus, 1 job(s)"


def test_render_cluster_unreachable_is_one_red_line() -> None:
    cluster = {"reachable": False, "scheduler_url": None, "error": "connection refused", "capacity": None}
    lines = render_cluster(cluster)
    assert len(lines) == 1
    assert lines[0].plain == "cluster: unreachable (connection refused)"
    assert lines[0].style == "red"


def test_render_sccache_cache_running_daemon() -> None:
    daemon = {
        "container": "abc123",
        "error": None,
        "cache_hits": 100,
        "cache_misses": 40,
        "distributed": 30,
        "dist_errors": 1,
        "cache_location": None,
        "per_node": {},
        "verdict": "DISTRIBUTING",
    }
    text = render_sccache_cache(daemon)
    plain = text.plain
    assert "DISTRIBUTING" in plain
    assert "cache" in plain
    assert "dist" in plain


def test_render_sccache_cache_none_is_no_container() -> None:
    text = render_sccache_cache(None)
    assert text.plain == "daemon: no build container running"


def test_render_sccache_cache_error_is_stats_unavailable() -> None:
    daemon = {
        "container": "abc123",
        "error": "stats query failed: boom",
        "cache_hits": 0,
        "cache_misses": 0,
        "distributed": 0,
        "dist_errors": 0,
        "cache_location": None,
        "per_node": {},
        "verdict": "stats unavailable",
    }
    text = render_sccache_cache(daemon)
    assert "stats unavailable" in text.plain


def test_render_ccache_cache_dict() -> None:
    text = render_ccache_cache({"cache_hits": 10, "cache_misses": 4, "hit_rate": 71.4})
    plain = text.plain
    assert "10/4" in plain
    assert "71% hit" in plain


def test_render_ccache_cache_none_is_stats_unavailable() -> None:
    text = render_ccache_cache(None)
    assert isinstance(text, Text)
    assert text.plain == "ccache: stats unavailable"


def test_daemon_doc_carries_per_language_dicts() -> None:
    """daemon_doc surfaces the report's per-language hit/miss dicts alongside per_node."""
    report = BuildDaemonReport(
        running=True,
        container="abc123",
        cache_hits=52697,
        cache_misses=4333,
        distributed=4000,
        dist_errors=2,
        per_node=(("10.42.0.2:10501", 4000),),
        cache_hits_by_lang={"C/C++": 52186, "Rust": 511},
        cache_misses_by_lang={"C/C++": 4263, "Assembler": 70},
    )
    doc = daemon_doc(report)
    assert doc is not None
    assert doc["hits_by_lang"] == {"C/C++": 52186, "Rust": 511}
    assert doc["misses_by_lang"] == {"C/C++": 4263, "Assembler": 70}
    # The scalar aggregates and per_node distribution remain present.
    assert doc["cache_hits"] == 52697
    assert doc["per_node"] == {"10.42.0.2:10501": 4000}


def test_render_sccache_cache_emits_a_line_per_language() -> None:
    """render_sccache_cache prints one hit/miss/hit-rate line for each language present."""
    daemon = {
        "container": "abc123",
        "error": None,
        "cache_hits": 152,
        "cache_misses": 50,
        "distributed": 40,
        "dist_errors": 1,
        "cache_location": None,
        "per_node": {"10.42.0.2:10501": 40},
        "hits_by_lang": {"C/C++": 100, "Rust": 52},
        "misses_by_lang": {"C/C++": 40, "Rust": 10},
        "verdict": "DISTRIBUTING",
    }
    lines = render_sccache_cache(daemon).plain.splitlines()
    cpp_line = next(ln for ln in lines if "C/C++" in ln)
    rust_line = next(ln for ln in lines if "Rust" in ln)
    # C/C++: 100 hits, 40 misses -> 100/140 = 71% hit.
    assert "100/40 hit/miss" in cpp_line
    assert "71% hit" in cpp_line
    # Rust: 52 hits, 10 misses -> 52/62 = 84% hit.
    assert "52/10 hit/miss" in rust_line
    assert "84% hit" in rust_line
    # The per-node distribution is rendered too.
    assert any("10.42.0.2:10501" in ln for ln in lines)


def test_cache_backend_badge_sccache_is_green_sitemap() -> None:
    glyph, colour = cache_backend_badge("sccache")
    assert glyph == _BACKEND_SITEMAP_GLYPH
    assert colour == "green"


def test_cache_backend_badge_ccache_is_cyan_database() -> None:
    glyph, colour = cache_backend_badge("ccache")
    assert glyph == _BACKEND_DATABASE_GLYPH
    assert colour == "cyan"


def test_cache_backend_badge_none_state_is_dim_red_ban() -> None:
    """The classified "none" backend (no cache active) gets the visible ban badge."""
    glyph, colour = cache_backend_badge("none")
    assert glyph == _BACKEND_BAN_GLYPH
    assert colour == "dim red"


def test_cache_backend_badge_unclassified_is_empty() -> None:
    """An unclassified backend (None) is distinct from the "none" state: empty glyph/colour."""
    assert cache_backend_badge(None) == ("", "")


def test_cache_backend_badge_glyphs_are_distinct() -> None:
    """Each classified state's glyph reads apart from the other two at a glance."""
    glyphs = {cache_backend_badge(backend)[0] for backend in ("sccache", "ccache", "none")}
    assert len(glyphs) == 3
