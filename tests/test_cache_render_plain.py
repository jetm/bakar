"""Tests for the plain-text render siblings in bakar.cache_render."""

from __future__ import annotations

from bakar.cache_render import (
    render_ccache_cache_plain,
    render_cluster_plain,
    render_sccache_cache_plain,
)

_ESC = "\x1b"


def _cluster_doc() -> dict:
    return {
        "reachable": True,
        "scheduler_url": "http://localhost:10600",
        "error": None,
        "capacity": {
            "num_servers": 2,
            "num_cpus": 48,
            "in_progress": 7,
            "servers": [{"id": "pc2", "num_cpus": 32, "in_progress": 4}],
        },
    }


def _daemon_doc() -> dict:
    return {
        "container": "avocado-build",
        "error": None,
        "cache_hits": 100,
        "cache_misses": 40,
        "distributed": 25,
        "dist_errors": 1,
        "cache_location": "/cache",
        "per_node": {"pc2": 12},
        "hits_by_lang": {"c": 80},
        "misses_by_lang": {"c": 30, "rust": 10},
        "verdict": "DISTRIBUTING",
    }


def test_cluster_plain_carries_counts_no_ansi() -> None:
    lines = render_cluster_plain(_cluster_doc())
    joined = "\n".join(lines)
    assert _ESC not in joined
    assert "2 server(s)" in lines[0]
    assert "48 cpus" in lines[0]
    # per-node line present
    assert any("pc2" in ln and "32 cpus" in ln for ln in lines[1:])


def test_cluster_plain_unreachable() -> None:
    doc = {"reachable": False, "error": "connection refused", "capacity": None}
    lines = render_cluster_plain(doc)
    assert lines == ["cluster: unreachable (connection refused)"]


def test_sccache_plain_carries_fields_and_per_node_no_ansi() -> None:
    text = render_sccache_cache_plain(_daemon_doc())
    assert _ESC not in text
    assert "DISTRIBUTING" in text
    assert "100/40 hit/miss" in text
    assert "dist[pc2]: 12 job(s)" in text
    assert "cache[rust]:" in text


def test_sccache_plain_no_daemon() -> None:
    assert render_sccache_cache_plain(None) == "daemon: no build container running"


def test_ccache_plain_line() -> None:
    text = render_ccache_cache_plain({"cache_hits": 90, "cache_misses": 10, "hit_rate": 90.0})
    assert _ESC not in text
    assert text == "ccache: 90/10 hit/miss (90% hit)"
