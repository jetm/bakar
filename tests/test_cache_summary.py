"""Unit tests for the build-end cache-summary and badge helpers.

Covers the plain/Rich build-end summary composers, the pure-``str`` and Rich
badge-token builders, and the per-build ``cache_delta`` helper. Plain-path
assertions verify the greppable ``bakar[cache]`` prefix and the absence of any
ANSI byte; no subprocess or daemon access is involved.
"""

from __future__ import annotations

import pytest
from rich.text import Text

from bakar.cache_render import (
    build_end_summary_plain,
    build_end_summary_rich,
    cache_badge_rich,
    cache_badge_token,
    cache_delta,
    cache_hit_pct,
    dist_badge_rich,
    dist_badge_token,
)

pytestmark = pytest.mark.unit


def _sccache_doc() -> dict:
    return {
        "cache_hits": 90,
        "cache_misses": 10,
        "distributed": 8,
        "dist_errors": 0,
        "per_node": {"10.42.0.2": 8},
        "hits_by_lang": {"C/C++": 90},
        "misses_by_lang": {"C/C++": 10},
        "verdict": "DISTRIBUTING",
        "error": None,
        "window": "build",
    }


def _ccache_doc() -> dict:
    return {"cache_hits": 5, "cache_misses": 2, "hit_rate": 71.0, "window": "build"}


def test_plain_summary_has_bakar_cache_prefix() -> None:
    summary = build_end_summary_plain(_ccache_doc(), "ccache")
    assert summary.startswith("bakar[cache]")


def test_plain_summary_has_no_ansi() -> None:
    for doc, backend in ((_ccache_doc(), "ccache"), (_sccache_doc(), "sccache")):
        summary = build_end_summary_plain(doc, backend)
        assert "\x1b" not in summary


def test_plain_summary_carries_backend_and_window_tokens() -> None:
    summary = build_end_summary_plain(_sccache_doc(), "sccache")
    assert "backend=sccache" in summary
    assert "window=build" in summary
    ccache_summary = build_end_summary_plain(_ccache_doc(), "ccache")
    assert "backend=ccache" in ccache_summary
    assert "window=build" in ccache_summary


def test_plain_summary_none_doc_is_empty() -> None:
    assert build_end_summary_plain(None, "ccache") == ""


def test_rich_summary_is_unprefixed_text() -> None:
    rich = build_end_summary_rich(_ccache_doc(), "ccache")
    assert isinstance(rich, Text)
    assert not rich.plain.startswith("bakar[cache]")


def test_cache_badge_token_formats_percent() -> None:
    assert cache_badge_token(90.0) == "cache=90%"


def test_sccache_hit_pct_guards_divide_by_zero() -> None:
    # Both counters zero must not raise ZeroDivisionError.
    assert cache_hit_pct(0, 0) == 0.0
    assert cache_badge_token(cache_hit_pct(0, 0)) == "cache=0%"
    assert cache_hit_pct(90, 10) == 90.0


def test_dist_badge_token_maps_verdict() -> None:
    assert dist_badge_token("DISTRIBUTING") == "dist=on"
    assert dist_badge_token("LOCAL-ONLY") == "dist=off"
    assert dist_badge_token("idle (no compiles yet)") == "dist=idle"
    assert dist_badge_token(None) == "dist=idle"


def test_rich_badges_are_text() -> None:
    assert isinstance(cache_badge_rich(90.0), Text)
    dist = dist_badge_rich("LOCAL-ONLY")
    assert isinstance(dist, Text)
    assert "off" in dist.plain


def test_cache_delta_subtracts_baseline() -> None:
    first = {"cache_hits": 40, "cache_misses": 4, "hit_rate": 90.9, "window": "build"}
    last = {"cache_hits": 90, "cache_misses": 10, "hit_rate": 90.0, "window": "build"}
    delta = cache_delta(first, last)
    assert delta["cache_hits"] == 50
    assert delta["cache_misses"] == 6
    assert delta["window"] == "build"
    # hit_rate recomputed from the delta counts, not carried from last.
    assert delta["hit_rate"] == pytest.approx(100.0 * 50 / 56)


def test_cache_delta_none_baseline_is_lifetime() -> None:
    last = {"cache_hits": 90, "cache_misses": 10, "hit_rate": 90.0}
    delta = cache_delta(None, last)
    assert delta["window"] == "lifetime"
    assert delta["cache_hits"] == 90
    assert delta["cache_misses"] == 10


def test_cache_delta_clamps_negative_counts() -> None:
    # A mid-build counter reset makes last < first; the delta must clamp to 0.
    first = {"cache_hits": 90, "cache_misses": 10, "hit_rate": 90.0}
    last = {"cache_hits": 5, "cache_misses": 1, "hit_rate": 83.3}
    delta = cache_delta(first, last)
    assert delta["cache_hits"] >= 0
    assert delta["cache_misses"] >= 0
    assert delta["cache_hits"] == 0
    assert delta["cache_misses"] == 0


def test_cache_delta_subtracts_per_lang_and_per_node() -> None:
    first = {
        "cache_hits": 10,
        "cache_misses": 1,
        "distributed": 2,
        "dist_errors": 0,
        "hits_by_lang": {"C/C++": 10},
        "misses_by_lang": {"C/C++": 1},
        "per_node": {"10.42.0.2": 2},
        "verdict": "DISTRIBUTING",
    }
    last = {
        "cache_hits": 90,
        "cache_misses": 10,
        "distributed": 8,
        "dist_errors": 1,
        "hits_by_lang": {"C/C++": 90},
        "misses_by_lang": {"C/C++": 10},
        "per_node": {"10.42.0.2": 8},
        "verdict": "DISTRIBUTING",
    }
    delta = cache_delta(first, last)
    assert delta["distributed"] == 6
    assert delta["dist_errors"] == 1
    assert delta["hits_by_lang"] == {"C/C++": 80}
    assert delta["misses_by_lang"] == {"C/C++": 9}
    assert delta["per_node"] == {"10.42.0.2": 6}
    assert delta["window"] == "build"


def test_cache_delta_none_last_returns_none() -> None:
    assert cache_delta(None, None) is None
    assert cache_delta({"cache_hits": 1}, None) is None
