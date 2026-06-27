"""Unit tests for the topology+RAM-aware parallelism derivation.

:mod:`bakar.tuning` derives ``PARALLEL_MAKE`` and ``BB_NUMBER_THREADS`` from the
local CPU count, host RAM, the active compiler launcher, and (for sccache-dist)
the remote cluster CPU count. ``derive_parallelism`` is a pure function; the
host probes (``host_ram_gb``) read ``/proc/meminfo`` best-effort.
"""

from __future__ import annotations

import pytest

from bakar.tuning import PER_TASK_GB, derive_parallelism

pytestmark = pytest.mark.unit


@pytest.mark.unit
def test_sccache_dist_single_node_feeds_cluster_cpus() -> None:
    """sccache-dist with one 32-cpu node on a 32t/96GB host: PM=32, BBNT=24."""
    plan = derive_parallelism(nproc_local=32, ram_gb=96.0, launcher="sccache-dist", cluster_cpus=32)

    assert plan.parallel_make == 32
    assert plan.bb_number_threads == 24


@pytest.mark.unit
def test_sccache_dist_two_nodes_feeds_full_cluster() -> None:
    """sccache-dist with a 64-cpu cluster: PM=64, BBNT still RAM-bound at 24."""
    plan = derive_parallelism(nproc_local=32, ram_gb=96.0, launcher="sccache-dist", cluster_cpus=64)

    assert plan.parallel_make == 64
    assert plan.bb_number_threads == 24


@pytest.mark.unit
def test_ccache_ignores_cluster_cpus() -> None:
    """ccache routes locally: PM=nproc_local even if cluster_cpus is passed."""
    plan = derive_parallelism(nproc_local=32, ram_gb=96.0, launcher="ccache", cluster_cpus=64)

    assert plan.parallel_make == 32
    assert plan.bb_number_threads == 24


@pytest.mark.unit
def test_no_launcher_uses_local_nproc() -> None:
    """launcher=none: PM=nproc_local, cluster_cpus ignored."""
    plan = derive_parallelism(nproc_local=32, ram_gb=96.0, launcher="none", cluster_cpus=64)

    assert plan.parallel_make == 32
    assert plan.bb_number_threads == 24


@pytest.mark.unit
def test_ram_cap_dominates_thread_count() -> None:
    """32t/64GB: BBNT capped by floor(64/4)=16 below nproc."""
    plan = derive_parallelism(nproc_local=32, ram_gb=64.0, launcher="ccache", cluster_cpus=None)

    assert plan.bb_number_threads == 16


@pytest.mark.unit
def test_nproc_cap_dominates_thread_count() -> None:
    """8t/96GB: BBNT capped by nproc=8 below floor(96/4)=24."""
    plan = derive_parallelism(nproc_local=8, ram_gb=96.0, launcher="none", cluster_cpus=None)

    assert plan.bb_number_threads == 8


@pytest.mark.unit
def test_tiny_ram_clamps_threads_to_one() -> None:
    """4t/2GB: floor(2/4)=0 clamps up to 1, never zero."""
    plan = derive_parallelism(nproc_local=4, ram_gb=2.0, launcher="none", cluster_cpus=None)

    assert plan.bb_number_threads == 1


@pytest.mark.unit
def test_single_cpu_host_thread_count_at_least_one() -> None:
    """nproc_local=1 keeps BBNT >= 1."""
    plan = derive_parallelism(nproc_local=1, ram_gb=96.0, launcher="none", cluster_cpus=None)

    assert plan.bb_number_threads == 1
    assert plan.parallel_make == 1


@pytest.mark.unit
def test_sccache_dist_without_cluster_cpus_falls_back_to_nproc() -> None:
    """sccache-dist but cluster unreachable (cluster_cpus=None): PM=nproc_local."""
    plan = derive_parallelism(nproc_local=32, ram_gb=96.0, launcher="sccache-dist", cluster_cpus=None)

    assert plan.parallel_make == 32


@pytest.mark.unit
def test_sccache_dist_zero_cluster_cpus_falls_back_to_nproc() -> None:
    """A zero cluster_cpus (degenerate probe) falls back to nproc_local for PM."""
    plan = derive_parallelism(nproc_local=32, ram_gb=96.0, launcher="sccache-dist", cluster_cpus=0)

    assert plan.parallel_make == 32


@pytest.mark.unit
def test_plan_is_frozen() -> None:
    """ParallelismPlan is a frozen dataclass (immutable)."""
    import dataclasses

    plan = derive_parallelism(nproc_local=32, ram_gb=96.0, launcher="none", cluster_cpus=None)

    assert dataclasses.is_dataclass(plan)
    with pytest.raises(dataclasses.FrozenInstanceError):
        plan.parallel_make = 1  # type: ignore[misc]


@pytest.mark.unit
def test_rationale_is_nonempty_string() -> None:
    """The rationale explains the chosen numbers in human terms."""
    plan = derive_parallelism(nproc_local=32, ram_gb=96.0, launcher="sccache-dist", cluster_cpus=64)

    assert isinstance(plan.rationale, str)
    assert plan.rationale
    assert "64" in plan.rationale
    assert "24" in plan.rationale


@pytest.mark.unit
def test_per_task_gb_constant() -> None:
    """PER_TASK_GB is the documented 4.0 GB per concurrent recipe estimate."""
    assert PER_TASK_GB == 4.0


@pytest.mark.unit
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


@pytest.mark.unit
def test_host_ram_gb_fallback_when_unreadable(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An absent /proc/meminfo yields the 16.0 GB fallback rather than raising."""
    from pathlib import Path

    from bakar import tuning

    monkeypatch.setattr(tuning, "_MEMINFO_PATH", Path(tmp_path / "absent"))

    assert tuning.host_ram_gb() == 16.0
