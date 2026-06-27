"""Unit tests for ``bakar.cache_render``.

The render helpers turn plain dicts (the shape the doc helpers emit) into Rich
``Text``; assertions read ``.plain`` so they ignore styling. No subprocess or
cluster access is involved.
"""

from __future__ import annotations

import pytest
from rich.text import Text

from bakar.cache_render import render_ccache_cache, render_cluster, render_sccache_cache

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
