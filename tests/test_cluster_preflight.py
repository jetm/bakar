"""Cluster-mode preflight: gating flag + cluster-only doctor checks.

These tests defend the cluster-off default: the `cluster` flag resolves False
when absent, and the cluster preflight checks are filtered out of `doctor`
entirely (no result row, no probe) unless it is on. When on, the central
hashserv/prserv checks classify reachable/unreachable/unset/loopback endpoints.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from bakar.config import BuildConfig, resolve
from bakar.diagnostics import (
    _DOCKER_CHECKS,
    CHECK_GROUPS,
    Severity,
    Status,
    check_central_hashserv,
    check_central_prserv,
    check_shared_cache_mounts,
    run_all,
)
from bakar.user_config import UserConfig
from bakar.workspace_config import WorkspaceConfig


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip ambient toggles + cache-dir env overrides so each test is hermetic.

    SSTATE_DIR/DL_DIR/CCACHE_DIR would otherwise override the effective dirs the
    shared-mount check validates, pointing it at the real host caches instead of
    the test's tmp_path.
    """
    for var in ("BAKAR_CLUSTER", "KAS_CONTAINER_IMAGE", "SSTATE_DIR", "DL_DIR", "CCACHE_DIR"):
        monkeypatch.delenv(var, raising=False)


def _workspace(tmp_path: Path) -> Path:
    """A workspace path with the nxp subdir present (resolve() needs it)."""
    (tmp_path / "nxp").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _cfg(**over: object) -> BuildConfig:
    """A minimal BuildConfig for calling a check function directly."""
    base: dict[str, object] = {
        "workspace": Path("/tmp"),
        "bsp_family": "nxp",
        "machine": "m",
        "distro": "d",
        "image": "i",
        "manifest": "x.xml",
        "repo_url": "https://example.com",
        "repo_branch": "main",
        "kas_container_image": "img:latest",
    }
    base.update(over)
    return BuildConfig(**base)  # type: ignore[arg-type]


# --- Task 1: config flag ---------------------------------------------------


@pytest.mark.unit
def test_config_default_absent_key_is_non_cluster(tmp_path: Path) -> None:
    """An absent `cluster` key resolves to a non-cluster build."""
    cfg = resolve(
        workspace=_workspace(tmp_path),
        bsp_family="nxp",
        user_config=UserConfig(),
        workspace_config=WorkspaceConfig(),
    )
    assert cfg.cluster is False


@pytest.mark.unit
def test_config_default_cluster_true_when_set(tmp_path: Path) -> None:
    """`cluster = true` resolves to a cluster build."""
    cfg = resolve(
        workspace=_workspace(tmp_path),
        bsp_family="nxp",
        user_config=UserConfig(cluster=True),
        workspace_config=WorkspaceConfig(),
    )
    assert cfg.cluster is True


# --- Task 4: gating (filter) + central-service checks ----------------------


@pytest.mark.unit
def test_gating_central_absent_when_cluster_off(tmp_path: Path) -> None:
    """cluster=False: run_all lists no central check and probes nothing."""
    cfg = resolve(
        workspace=_workspace(tmp_path),
        bsp_family="nxp",
        user_config=UserConfig(cluster=False),
        workspace_config=WorkspaceConfig(),
    )
    names = {r.name for r in run_all(cfg)}
    assert "central-hashserv" not in names
    assert "central-prserv" not in names


@pytest.mark.unit
def test_gating_central_present_when_cluster_on(tmp_path: Path) -> None:
    """cluster=True: the central checks appear in the diagnosis."""
    cfg = resolve(
        workspace=_workspace(tmp_path),
        bsp_family="nxp",
        user_config=UserConfig(cluster=True),
        workspace_config=WorkspaceConfig(),
    )
    names = {r.name for r in run_all(cfg)}
    assert "central-hashserv" in names
    assert "central-prserv" in names


@pytest.mark.unit
def test_central_hashserv_reachable_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    """A reachable central hashserv -> PASS at BLOCK."""
    monkeypatch.setattr("bakar.hashserv.central_listening", lambda *_a, **_k: True)
    result = check_central_hashserv(_cfg(cluster=True, bb_hashserve="10.42.0.1:8686"))
    assert result.status == Status.PASS
    assert result.severity == Severity.BLOCK


@pytest.mark.unit
def test_central_hashserv_unreachable_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    """A refused central hashserv -> FAIL at BLOCK."""
    monkeypatch.setattr("bakar.hashserv.central_listening", lambda *_a, **_k: False)
    result = check_central_hashserv(_cfg(cluster=True, bb_hashserve="10.42.0.1:8686"))
    assert result.status == Status.FAIL
    assert result.severity == Severity.BLOCK


@pytest.mark.unit
def test_central_hashserv_unset_warns_not_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    """cluster on with no hashserv endpoint -> FAIL at WARN (surfaced, not blocking), no probe."""

    def _boom(*_a: object, **_k: object) -> bool:
        raise AssertionError("probed a central endpoint that was unset")

    monkeypatch.setattr("bakar.hashserv.central_listening", _boom)
    result = check_central_hashserv(_cfg(cluster=True, bb_hashserve=None))
    assert result.status == Status.FAIL
    assert result.severity == Severity.WARN


@pytest.mark.unit
def test_central_hashserv_loopback_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    """A loopback central endpoint -> WARN (valid on the hub, breaks when reused)."""

    def _boom(*_a: object, **_k: object) -> bool:
        raise AssertionError("probed a loopback endpoint")

    monkeypatch.setattr("bakar.hashserv.central_listening", _boom)
    result = check_central_hashserv(_cfg(cluster=True, bb_hashserve="127.0.0.1:8686"))
    assert result.status == Status.FAIL
    assert result.severity == Severity.WARN


@pytest.mark.unit
def test_central_prserv_reachable_and_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """prserv mirrors hashserv: reachable -> PASS/BLOCK; unset -> FAIL/WARN."""
    monkeypatch.setattr("bakar.prserv.central_listening", lambda *_a, **_k: True)
    reachable = check_central_prserv(_cfg(cluster=True, prserv_host="10.42.0.1:8585"))
    assert reachable.status == Status.PASS
    assert reachable.severity == Severity.BLOCK

    unset = check_central_prserv(_cfg(cluster=True, prserv_host=None))
    assert unset.status == Status.FAIL
    assert unset.severity == Severity.WARN


@pytest.mark.unit
def test_central_checks_host_pure_and_grouped_once() -> None:
    """The central checks are not docker-gated and land in exactly the Cluster group."""
    assert check_central_hashserv not in _DOCKER_CHECKS
    assert check_central_prserv not in _DOCKER_CHECKS
    for cname in ("central-hashserv", "central-prserv"):
        buckets = [group for group, names in CHECK_GROUPS if cname in names]
        assert buckets == ["Cluster"], f"{cname} grouped into {buckets}"


# --- Task 5: shared-mount check --------------------------------------------

_NFS_ENTRY = ("10.42.0.1:/export/sstate", "/mnt/sstate", "nfs", "rw,hard,vers=4")


@pytest.mark.unit
def test_gating_shared_mount_absent_when_cluster_off(tmp_path: Path) -> None:
    """cluster=False: run_all lists no shared-mounts check."""
    cfg = resolve(
        workspace=_workspace(tmp_path),
        bsp_family="nxp",
        user_config=UserConfig(cluster=False),
        workspace_config=WorkspaceConfig(),
    )
    assert "shared-mounts" not in {r.name for r in run_all(cfg)}


@pytest.mark.unit
def test_gating_shared_mount_present_when_cluster_on(tmp_path: Path) -> None:
    """cluster=True: the shared-mounts check appears."""
    cfg = resolve(
        workspace=_workspace(tmp_path),
        bsp_family="nxp",
        user_config=UserConfig(cluster=True),
        workspace_config=WorkspaceConfig(),
    )
    assert "shared-mounts" in {r.name for r in run_all(cfg)}


@pytest.mark.unit
def test_shared_mount_nfs_writable_passes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A writable NFS-mounted shared dir -> PASS at BLOCK, message names the source."""
    monkeypatch.setattr("bakar.diagnostics._mount_entry_in", lambda *_a: _NFS_ENTRY)
    result = check_shared_cache_mounts(_cfg(cluster=True, sstate_dir=str(tmp_path), dl_dir=None, ccache=False))
    assert result.status == Status.PASS
    assert result.severity == Severity.BLOCK
    assert "10.42.0.1:/export/sstate" in result.message


@pytest.mark.unit
def test_shared_mount_local_fs_blocks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A shared dir on a local (non-NFS) filesystem -> FAIL at BLOCK."""
    monkeypatch.setattr("bakar.diagnostics._mount_entry_in", lambda *_a: ("/dev/sda1", "/", "ext4", "rw"))
    result = check_shared_cache_mounts(_cfg(cluster=True, sstate_dir=str(tmp_path), dl_dir=None, ccache=False))
    assert result.status == Status.FAIL
    assert result.severity == Severity.BLOCK
    assert "not an NFS mount" in result.message


@pytest.mark.unit
def test_shared_mount_missing_blocks(tmp_path: Path) -> None:
    """A missing shared dir -> FAIL at BLOCK naming it missing."""
    result = check_shared_cache_mounts(_cfg(cluster=True, sstate_dir=str(tmp_path / "nope"), dl_dir=None, ccache=False))
    assert result.status == Status.FAIL
    assert result.severity == Severity.BLOCK
    assert "missing" in result.message


@pytest.mark.unit
@pytest.mark.skipif(os.geteuid() == 0, reason="chmod is ignored as root; write probe would succeed")
def test_shared_mount_unwritable_blocks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An existing but unwritable shared dir -> FAIL at BLOCK, distinguished from missing."""
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    try:
        monkeypatch.setattr("bakar.diagnostics._mount_entry_in", lambda *_a: _NFS_ENTRY)
        result = check_shared_cache_mounts(_cfg(cluster=True, sstate_dir=str(ro), dl_dir=None, ccache=False))
        assert result.status == Status.FAIL
        assert result.severity == Severity.BLOCK
        assert "not writable" in result.message
    finally:
        ro.chmod(0o700)


@pytest.mark.unit
def test_shared_mount_soft_option_warns(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A soft-mounted shared dir -> WARN (data-integrity hazard), not a block."""
    monkeypatch.setattr(
        "bakar.diagnostics._mount_entry_in", lambda *_a: ("10.42.0.1:/e", "/mnt", "nfs", "rw,soft,vers=4")
    )
    result = check_shared_cache_mounts(_cfg(cluster=True, sstate_dir=str(tmp_path), dl_dir=None, ccache=False))
    assert result.status == Status.FAIL
    assert result.severity == Severity.WARN
    assert "soft" in result.message


@pytest.mark.unit
def test_shared_mount_clock_skew_warns(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A large mtime-vs-local-clock delta on the shared mount -> WARN."""
    monkeypatch.setattr("bakar.diagnostics._mount_entry_in", lambda *_a: _NFS_ENTRY)
    monkeypatch.setattr("bakar.diagnostics.time.time", lambda: 10_000_000_000.0)
    result = check_shared_cache_mounts(_cfg(cluster=True, sstate_dir=str(tmp_path), dl_dir=None, ccache=False))
    assert result.status == Status.FAIL
    assert result.severity == Severity.WARN
    assert "skew" in result.message


@pytest.mark.unit
def test_shared_mount_local_ccache_warns_not_blocks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A local (non-NFS) ccache dir is a WARN, not a BLOCK - local ccache is legitimate."""
    ccache = tmp_path / "ccache"
    ccache.mkdir()
    monkeypatch.setattr("bakar.diagnostics._mount_entry_in", lambda *_a: ("/dev/sda1", "/", "ext4", "rw"))
    result = check_shared_cache_mounts(
        _cfg(cluster=True, sstate_dir=None, dl_dir=None, ccache=True, ccache_dir=str(ccache))
    )
    assert result.severity == Severity.WARN
    assert "ccache_dir" in result.message


@pytest.mark.unit
def test_central_hashserv_bracketed_ipv6_loopback_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bracketed IPv6 loopback central endpoint is caught as loopback (WARN, no probe)."""

    def _boom(*_a: object, **_k: object) -> bool:
        raise AssertionError("probed a bracketed IPv6 loopback endpoint")

    monkeypatch.setattr("bakar.hashserv.central_listening", _boom)
    result = check_central_hashserv(_cfg(cluster=True, bb_hashserve="[::1]:8686"))
    assert result.status == Status.FAIL
    assert result.severity == Severity.WARN
