"""Tests for the sccache-dist scheduler/client log parsers.

Line formats are the real ones emitted by the R0 instrumentation (sccache fork),
captured from a live cold build.
"""

from __future__ import annotations

import pytest

from bakar.sched_triage import parse_client_log, parse_dist_alloc, parse_dist_status

pytestmark = pytest.mark.unit


def test_parse_dist_alloc_flags_idle_skip_misroute() -> None:
    """Choosing a loaded server while an idle candidate exists is a misroute + idle skip."""
    lines = [
        "dist-alloc: job 1 -> ServerId(192.168.8.172:10501) (now 6/32 jobs, load 0.1875); "
        "candidates [(ServerId(192.168.8.172:10501), 0.15625, 5, 32), (ServerId(10.42.0.2:10501), 0.0, 0, 32)]",
    ]
    stats = parse_dist_alloc(lines)
    assert stats.total == 1
    assert stats.misroutes == 1
    assert stats.idle_skips == 1
    assert stats.per_node_chosen["192.168.8.172:10501"] == 1


def test_parse_dist_alloc_least_loaded_choice_is_not_misroute() -> None:
    """Choosing the least-loaded candidate is not a misroute.

    Real-shape line: the ``(now 5/32, load 0.15625)`` clause is the POST-assignment
    load, but the chosen server's PRE-load in candidates is 0.125 - the least
    loaded - so this must NOT be flagged (the bug that read the post-load did).
    """
    lines = [
        "dist-alloc: job 2 -> ServerId(10.42.0.2:10501) (now 5/32 jobs, load 0.15625); "
        "candidates [(ServerId(192.168.8.172:10501), 0.15625, 5, 32), (ServerId(10.42.0.2:10501), 0.125, 4, 32)]",
    ]
    stats = parse_dist_alloc(lines)
    assert stats.total == 1
    assert stats.misroutes == 0
    assert stats.idle_skips == 0


def test_parse_dist_alloc_misroute_pct() -> None:
    """misroute_pct is misroutes over total allocs."""
    lines = [
        "dist-alloc: job 1 -> ServerId(192.168.8.172:10501) (now 6/32 jobs, load 0.6); "
        "candidates [(ServerId(192.168.8.172:10501), 0.5, 5, 32), (ServerId(10.42.0.2:10501), 0.1, 1, 32)]",
        "dist-alloc: job 2 -> ServerId(10.42.0.2:10501) (now 2/32 jobs, load 0.125); "
        "candidates [(ServerId(10.42.0.2:10501), 0.1, 1, 32), (ServerId(192.168.8.172:10501), 0.5, 5, 32)]",
    ]
    stats = parse_dist_alloc(lines)
    assert stats.total == 2
    assert stats.misroutes == 1
    assert stats.misroute_pct == 50.0


def test_parse_dist_alloc_excludes_truncated_candidate_lines() -> None:
    """A candidate list shorter than the widest seen was cut by the load==0 break; exclude it."""
    lines = [
        "dist-alloc: job 1 -> ServerId(192.168.8.172:10501) (now 1/32 jobs, load 0.0); "
        "candidates [(ServerId(192.168.8.172:10501), 0.0, 0, 32)]",
        "dist-alloc: job 2 -> ServerId(192.168.8.172:10501) (now 6/32 jobs, load 0.6); "
        "candidates [(ServerId(192.168.8.172:10501), 0.5, 5, 32), (ServerId(10.42.0.2:10501), 0.1, 1, 32)]",
    ]
    stats = parse_dist_alloc(lines)
    assert stats.truncated == 1
    assert stats.total == 1
    assert stats.per_node_chosen["192.168.8.172:10501"] == 2


def test_parse_dist_alloc_buckets_misroute_by_load() -> None:
    """Misroutes are bucketed by concurrent in-flight so the load-dependent rate is visible."""
    lines = [
        "dist-alloc: job 1 -> ServerId(192.168.8.172:10501) (now 31/32 jobs, load 0.94); "
        "candidates [(ServerId(192.168.8.172:10501), 0.6, 30, 32), (ServerId(10.42.0.2:10501), 0.2, 20, 32)]",
    ]
    stats = parse_dist_alloc(lines)
    assert stats.misroutes_by_bucket["high"] == 1
    assert stats.total_by_bucket["high"] == 1
    assert stats.misroutes_by_bucket["low"] == 0


def test_parse_dist_status_computes_util_and_idle() -> None:
    """Utilisation and idle share are computed per poll against the poll's own ceiling."""
    lines = [
        'dist-status poll: 0 in-progress jobs; servers [("192.168.8.172:10501", 0, 32), ("10.42.0.2:10501", 0, 32)]',
        'dist-status poll: 32 in-progress jobs; servers [("192.168.8.172:10501", 16, 32), ("10.42.0.2:10501", 16, 32)]',
    ]
    stats = parse_dist_status(lines)
    assert stats.samples == 2
    assert stats.ceiling == 64
    assert stats.mean_inflight == 16.0
    # utils: 0/64=0, 32/64=0.5 -> mean 25%.
    assert stats.mean_util_pct == 25.0
    assert stats.idle_pct == 50.0  # one of two polls had 0 in-progress


def test_parse_dist_status_near_sat_uses_admission_ceiling() -> None:
    """near_sat is measured against the scheduler's admission ceiling, not raw cores.

    The scheduler admits up to ``cores_plus_slack = c + 1 + c//8`` per server, so
    two 32-core servers admit 2*(32+1+4) = 74, not the raw 64. An inflight of 60
    is >= 7/8*64 (56) under the old raw denominator but < 7/8*74 (~64.75), so it
    must NOT count as near-saturated.
    """
    lines = [
        'dist-status poll: 60 in-progress jobs; servers [("192.168.8.172:10501", 30, 32), ("10.42.0.2:10501", 30, 32)]',
    ]
    stats = parse_dist_status(lines)
    assert stats.admission_ceiling == 74
    assert stats.near_sat_pct == 0.0


def test_parse_client_log_job_timers() -> None:
    """dist-job lines are parsed into per-node counts and phase-timer means."""
    lines = [
        "[m_spacing.o]: dist-job done on 192.168.8.172:10501 in 5841ms "
        "(put_tc 1335ms, alloc 200ms, submit 0ms, run+fetch 4305ms, in_flight 85)",
        "[m_global.o]: dist-job done on 10.42.0.2:10501 in 4000ms "
        "(put_tc 1000ms, alloc 100ms, submit 0ms, run+fetch 2900ms, in_flight 40)",
    ]
    stats = parse_client_log(lines)
    assert stats.jobs == 2
    assert stats.per_node_jobs["192.168.8.172:10501"] == 1
    assert stats.per_node_jobs["10.42.0.2:10501"] == 1
    assert stats.mean_total_ms == 4920.5
    assert stats.mean_run_fetch_ms == 3602.5
    assert stats.mean_preprocess_ms is None  # no preprocess field yet


def test_parse_client_log_reads_optional_preprocess_timer() -> None:
    """When the fork ships the W2 preprocess timer, it is parsed and averaged."""
    lines = [
        "[a.o]: dist-job done on 192.168.8.172:10501 in 100ms "
        "(preprocess 25ms, put_tc 10ms, alloc 5ms, submit 0ms, run+fetch 60ms, in_flight 3)",
    ]
    stats = parse_client_log(lines)
    assert stats.jobs == 1
    assert stats.mean_preprocess_ms == 25.0


def test_parse_client_log_counts_not_eligible_and_fallbacks() -> None:
    """Conftest local compiles are counted separately from gate-full fallbacks."""
    lines = [
        "[conftest.o]: Compiling locally (not eligible for distributed compilation)",
        "[sub1.o]: Could not perform distributed compile, falling back to local: "
        "Insufficient capacity across 2 available servers: Failed to allocate job",
    ]
    stats = parse_client_log(lines)
    assert stats.not_eligible == 1
    assert stats.fallback_reasons["gate-full (insufficient cluster capacity)"] == 1


def test_parse_client_log_surfaces_rust_error_codes() -> None:
    """rustc E0xxx diagnostics in the remote-stderr dump are counted (W3 signal)."""
    line = (
        '{"$message_type":"diagnostic","message":"failed to resolve: use of undeclared type `String`",'
        '"code":{"code":"E0433","explanation":"..."},"level":"error"}'
    )
    stats = parse_client_log([line])
    assert stats.rust_error_codes["E0433"] == 1
