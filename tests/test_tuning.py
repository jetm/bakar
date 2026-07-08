"""Unit tests for the topology+RAM-aware parallelism derivation.

:mod:`bakar.tuning` derives ``PARALLEL_MAKE`` and ``BB_NUMBER_THREADS`` from the
local CPU count, host RAM, the active compiler launcher, and (for sccache-dist)
the remote cluster CPU count. ``derive_parallelism`` is a pure function; the
host probes (``host_ram_gb``) read ``/proc/meminfo`` best-effort.
"""

from __future__ import annotations

import pytest

from bakar.tuning import PER_TASK_GB, SCCACHE_DIST_PER_TASK_GB, derive_parallelism

pytestmark = pytest.mark.unit


def test_sccache_dist_single_node_feeds_cluster_cpus() -> None:
    """sccache-dist, one 32-cpu node on a 32t/96GB host: PM=32, BBNT=floor(96/0.95)=101.

    Compile RAM is offloaded to the cluster, so BB_NUMBER_THREADS uses the
    smaller sccache-dist divisor and drops the local-nproc cap.
    """
    plan = derive_parallelism(nproc_local=32, ram_gb=96.0, launcher="sccache-dist", cluster_cpus=32)

    assert plan.parallel_make == 32
    assert plan.bb_number_threads == 101


def test_sccache_dist_two_nodes_feeds_full_cluster() -> None:
    """sccache-dist, 64-cpu cluster: PM=64, BBNT=floor(96/0.95)=101 (offloaded divisor)."""
    plan = derive_parallelism(nproc_local=32, ram_gb=96.0, launcher="sccache-dist", cluster_cpus=64)

    assert plan.parallel_make == 64
    assert plan.bb_number_threads == 101


def test_ccache_ignores_cluster_cpus() -> None:
    """ccache routes locally: PM=nproc_local even if cluster_cpus is passed."""
    plan = derive_parallelism(nproc_local=32, ram_gb=96.0, launcher="ccache", cluster_cpus=64)

    assert plan.parallel_make == 32
    # 96GB / 2.5GB-per-task = 38 RAM threads, capped at nproc 32.
    assert plan.bb_number_threads == 32


def test_no_launcher_uses_local_nproc() -> None:
    """launcher=none: PM=nproc_local, cluster_cpus ignored."""
    plan = derive_parallelism(nproc_local=32, ram_gb=96.0, launcher="none", cluster_cpus=64)

    assert plan.parallel_make == 32
    # 96GB / 2.5GB-per-task = 38 RAM threads, capped at nproc 32.
    assert plan.bb_number_threads == 32


def test_ram_cap_dominates_thread_count() -> None:
    """32t/64GB: BBNT capped by floor(64/2.5)=25 below nproc."""
    plan = derive_parallelism(nproc_local=32, ram_gb=64.0, launcher="ccache", cluster_cpus=None)

    assert plan.bb_number_threads == 25


def test_nproc_cap_dominates_thread_count() -> None:
    """8t/96GB: BBNT capped by nproc=8 below floor(96/2.5)=38."""
    plan = derive_parallelism(nproc_local=8, ram_gb=96.0, launcher="none", cluster_cpus=None)

    assert plan.bb_number_threads == 8


def test_tiny_ram_clamps_threads_to_one() -> None:
    """4t/2GB: floor(2/2.5)=0 clamps up to 1, never zero."""
    plan = derive_parallelism(nproc_local=4, ram_gb=2.0, launcher="none", cluster_cpus=None)

    assert plan.bb_number_threads == 1


def test_single_cpu_host_thread_count_at_least_one() -> None:
    """nproc_local=1 keeps BBNT >= 1."""
    plan = derive_parallelism(nproc_local=1, ram_gb=96.0, launcher="none", cluster_cpus=None)

    assert plan.bb_number_threads == 1
    assert plan.parallel_make == 1


def test_sccache_dist_without_cluster_cpus_falls_back_to_nproc() -> None:
    """sccache-dist but cluster unreachable (cluster_cpus=None): PM=nproc_local."""
    plan = derive_parallelism(nproc_local=32, ram_gb=96.0, launcher="sccache-dist", cluster_cpus=None)

    assert plan.parallel_make == 32


def test_sccache_dist_zero_cluster_cpus_falls_back_to_nproc() -> None:
    """A zero cluster_cpus (degenerate probe) falls back to nproc_local for PM."""
    plan = derive_parallelism(nproc_local=32, ram_gb=96.0, launcher="sccache-dist", cluster_cpus=0)

    assert plan.parallel_make == 32


def test_sccache_dist_relaxes_nproc_cap_to_core_multiple() -> None:
    """Under sccache-dist the local-nproc cap is relaxed to 4x nproc, not dropped:
    with nproc=8 and ram=96, BBNT is min(floor(96/0.95)=101, 4*8=32) = 32 - far above
    the plain 8-recipe cap, but still core-bounded so a thin host cannot OOM."""
    plan = derive_parallelism(nproc_local=8, ram_gb=96.0, launcher="sccache-dist", cluster_cpus=64)

    assert plan.bb_number_threads == 32


def test_sccache_dist_caps_threads_at_core_multiple_on_thin_host() -> None:
    """A high-RAM/low-core host is bounded by 4x nproc, not RAM, to avoid an OOM
    thread count: nproc=8/ram=256 -> min(floor(256/0.95)=269, 4*8=32) = 32."""
    plan = derive_parallelism(nproc_local=8, ram_gb=256.0, launcher="sccache-dist", cluster_cpus=64)

    assert plan.bb_number_threads == 32


def test_derive_parallelism_floors_nonpositive_nproc() -> None:
    """A non-positive nproc (unreadable /proc) floors to 1 rather than yielding PM=0."""
    plan = derive_parallelism(nproc_local=0, ram_gb=96.0, launcher="none", cluster_cpus=None)

    assert plan.parallel_make == 1
    assert plan.bb_number_threads == 1


def test_ccache_keeps_nproc_cap_at_same_inputs() -> None:
    """At nproc=8/ram=96, ccache keeps the tight nproc cap (BBNT=8) while sccache-dist
    relaxes it to 4x nproc (BBNT=32). Only the launcher differs - proves the dist path
    alone loosens (but does not drop) the local-core cap."""
    ccache = derive_parallelism(nproc_local=8, ram_gb=96.0, launcher="ccache", cluster_cpus=64)
    dist = derive_parallelism(nproc_local=8, ram_gb=96.0, launcher="sccache-dist", cluster_cpus=64)

    assert ccache.bb_number_threads == 8
    assert dist.bb_number_threads == 32


def test_sccache_dist_fallback_reverts_to_general_guard() -> None:
    """Cluster unreachable (cluster_cpus=None): compile runs local, so BBNT
    reverts to the general guard with the nproc cap - min(32, floor(96/2.5))=32."""
    plan = derive_parallelism(nproc_local=32, ram_gb=96.0, launcher="sccache-dist", cluster_cpus=None)

    assert plan.bb_number_threads == 32


def test_sccache_dist_tiny_ram_clamps_threads_to_one() -> None:
    """sccache-dist with 1GB RAM: floor(1/0.95)=1 stays at 1, never zero."""
    plan = derive_parallelism(nproc_local=32, ram_gb=1.0, launcher="sccache-dist", cluster_cpus=64)

    assert plan.bb_number_threads == 1


def test_sccache_dist_per_task_gb_constant() -> None:
    """The offloaded-compile divisor sits far below the general per-task estimate,
    reflecting that heavy compile RAM runs on the cluster, not locally."""
    assert SCCACHE_DIST_PER_TASK_GB == 0.95
    assert SCCACHE_DIST_PER_TASK_GB < PER_TASK_GB


def test_plan_is_frozen() -> None:
    """ParallelismPlan is a frozen dataclass (immutable)."""
    import dataclasses

    plan = derive_parallelism(nproc_local=32, ram_gb=96.0, launcher="none", cluster_cpus=None)

    assert dataclasses.is_dataclass(plan)
    with pytest.raises(dataclasses.FrozenInstanceError):
        plan.parallel_make = 1  # type: ignore[misc]


def test_rationale_is_nonempty_string() -> None:
    """The rationale explains the chosen numbers in human terms."""
    plan = derive_parallelism(nproc_local=32, ram_gb=96.0, launcher="sccache-dist", cluster_cpus=64)

    assert isinstance(plan.rationale, str)
    assert plan.rationale
    assert "64" in plan.rationale
    assert "101" in plan.rationale


def test_per_task_gb_constant() -> None:
    """PER_TASK_GB is the documented 2.5 GB per concurrent recipe estimate."""
    assert PER_TASK_GB == 2.5


def test_host_ram_gb_reads_meminfo(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """host_ram_gb parses MemTotal from /proc/meminfo (kB -> GB)."""
    from pathlib import Path

    from bakar import tuning

    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:       98765432 kB\nMemFree: 12345 kB\n")
    monkeypatch.setattr(tuning, "_MEMINFO_PATH", Path(meminfo))

    ram = tuning.host_ram_gb()

    # 98765432 kB / 1024 / 1024 ~= 94.2 GB
    assert 94.0 < ram < 95.0


def test_host_ram_gb_fallback_when_unreadable(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An absent /proc/meminfo yields the 16.0 GB fallback rather than raising."""
    from pathlib import Path

    from bakar import tuning

    monkeypatch.setattr(tuning, "_MEMINFO_PATH", Path(tmp_path / "absent"))

    assert tuning.host_ram_gb() == 16.0
