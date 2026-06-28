"""Tests for _build_env's smart parallelism derivation.

When ``[build] parallel_make`` / ``bb_number_threads`` are None, ``_build_env``
derives them via :func:`bakar.tuning.derive_parallelism` from the local CPU
count, host RAM, the active launcher, and (for sccache-dist) the cluster CPU
count, and exports them as ``BAKAR_PARALLEL_MAKE`` / ``BAKAR_BB_NUMBER_THREADS``.
An explicit cfg override always wins; a probe failure or script-gen
(``ensure_hashserv=False``) never breaks the env and never probes the cluster.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from bakar.config import BuildConfig

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


def _cfg(
    workspace: Path,
    *,
    nproc: int | None = None,
    parallel_make: int | None = None,
    bb_number_threads: int | None = None,
    sccache_dist: bool = False,
    ccache: bool = True,
    sccache_scheduler_url: str | None = None,
    host_mode: bool = True,
) -> BuildConfig:
    cfg = BuildConfig(
        workspace=workspace,
        bsp_family="nxp",  # type: ignore[arg-type]
        machine="imx8mp-var-dart",
        distro="fsl-imx-xwayland",
        image="core-image-minimal",
        manifest="imx-6.6.52-2.2.2.xml",
        repo_url="https://example.invalid/repo.git",
        repo_branch="scarthgap",
        kas_container_image="jetm/kas-build-env:latest",
        host_mode=host_mode,
        nproc=nproc,
        parallel_make=parallel_make,
        bb_number_threads=bb_number_threads,
        sccache_dist=sccache_dist,
        ccache=ccache,
        sccache_scheduler_url=sccache_scheduler_url,
    )
    # Host-mode _build_env now requires the bundled bitbake bin on the launch PATH.
    if host_mode:
        cfg.bitbake_bin_path.mkdir(parents=True, exist_ok=True)
    return cfg


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NPROC", raising=False)
    monkeypatch.delenv("BAKAR_PARALLEL_MAKE", raising=False)
    monkeypatch.delenv("BAKAR_BB_NUMBER_THREADS", raising=False)


def _patch_host(monkeypatch: pytest.MonkeyPatch, *, nproc: int, ram_gb: float) -> None:
    from bakar import tuning

    monkeypatch.setattr("bakar.steps.kas_build.os.cpu_count", lambda: nproc)
    monkeypatch.setattr(tuning, "host_ram_gb", lambda: ram_gb)


@pytest.mark.unit
def test_derives_both_vars_when_cfg_none_none_launcher(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No launcher, both cfg fields None: PM=nproc, BBNT=RAM-bound."""
    from bakar.steps.kas_build import _build_env

    _clear_env(monkeypatch)
    _patch_host(monkeypatch, nproc=32, ram_gb=96.0)
    cfg = _cfg(tmp_path, ccache=False)

    env = _build_env(cfg, ensure_hashserv=False)

    assert env["BAKAR_PARALLEL_MAKE"] == "32"
    assert env["BAKAR_BB_NUMBER_THREADS"] == "24"


@pytest.mark.unit
def test_explicit_overrides_win_over_derivation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When cfg fields are set, they pass through unchanged (no derivation)."""
    from bakar.steps.kas_build import _build_env

    _clear_env(monkeypatch)
    _patch_host(monkeypatch, nproc=32, ram_gb=96.0)
    cfg = _cfg(tmp_path, parallel_make=256, bb_number_threads=8)

    env = _build_env(cfg, ensure_hashserv=False)

    assert env["BAKAR_PARALLEL_MAKE"] == "256"
    assert env["BAKAR_BB_NUMBER_THREADS"] == "8"


@pytest.mark.unit
def test_derives_only_the_unset_field(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """parallel_make set, bb_number_threads None: only BBNT is derived."""
    from bakar.steps.kas_build import _build_env

    _clear_env(monkeypatch)
    _patch_host(monkeypatch, nproc=32, ram_gb=96.0)
    cfg = _cfg(tmp_path, parallel_make=128)

    env = _build_env(cfg, ensure_hashserv=False)

    assert env["BAKAR_PARALLEL_MAKE"] == "128"
    assert env["BAKAR_BB_NUMBER_THREADS"] == "24"


@pytest.mark.unit
def test_sccache_dist_feeds_cluster_cpus_into_parallel_make(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """sccache-dist with a reachable 64-cpu cluster: PM=64 (cluster), BBNT RAM-bound."""
    import types

    from bakar import diagnostics
    from bakar.steps import kas_build
    from bakar.steps.kas_build import _build_env

    _clear_env(monkeypatch)
    _patch_host(monkeypatch, nproc=32, ram_gb=96.0)
    # No real network: stub the cluster probe and the host server pre-start.
    monkeypatch.setattr(
        kas_build,
        "probe_cluster",
        lambda url: types.SimpleNamespace(
            reachable=True,
            capacity=diagnostics.ClusterCapacity(num_servers=2, num_cpus=64, in_progress=0),
            error=None,
        ),
    )
    monkeypatch.setattr(kas_build.sccache_server, "ensure_running", lambda url=None: True)
    cfg = _cfg(tmp_path, sccache_dist=True, ccache=False, sccache_scheduler_url="http://localhost:10600")

    env = _build_env(cfg, ensure_hashserv=True)

    assert env["BAKAR_PARALLEL_MAKE"] == "64"
    assert env["BAKAR_BB_NUMBER_THREADS"] == "24"


@pytest.mark.unit
def test_cluster_probe_failure_falls_back_to_nproc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A raising cluster probe never breaks the env; PM falls back to nproc."""
    from bakar.steps import kas_build
    from bakar.steps.kas_build import _build_env

    _clear_env(monkeypatch)
    _patch_host(monkeypatch, nproc=32, ram_gb=96.0)

    def _boom(url: str | None) -> object:
        raise OSError("scheduler unreachable")

    monkeypatch.setattr(kas_build, "probe_cluster", _boom)
    monkeypatch.setattr(kas_build.sccache_server, "ensure_running", lambda url=None: True)
    cfg = _cfg(tmp_path, sccache_dist=True, ccache=False, sccache_scheduler_url="http://localhost:10600")

    env = _build_env(cfg, ensure_hashserv=True)

    assert env["BAKAR_PARALLEL_MAKE"] == "32"
    assert env["BAKAR_BB_NUMBER_THREADS"] == "24"


@pytest.mark.unit
def test_dry_run_does_not_probe_cluster(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Script-gen (ensure_hashserv=False) never probes the cluster; PM falls back to nproc."""
    from bakar.steps import kas_build
    from bakar.steps.kas_build import _build_env

    _clear_env(monkeypatch)
    _patch_host(monkeypatch, nproc=32, ram_gb=96.0)

    calls: list[str | None] = []

    def _record(url: str | None) -> object:
        calls.append(url)
        raise AssertionError("probe_cluster must not run in dry-run")

    monkeypatch.setattr(kas_build, "probe_cluster", _record)
    cfg = _cfg(tmp_path, sccache_dist=True, ccache=False, sccache_scheduler_url="http://localhost:10600")

    env = _build_env(cfg, ensure_hashserv=False)

    assert calls == []
    assert env["BAKAR_PARALLEL_MAKE"] == "32"
