"""The ``cache-mounts`` doctor check and its probe-first gating in ``run_all``.

Covers the pure formatting helper ``_cache_mounts_result`` (SKIP/FAIL/PASS
shaping from already-computed ``CacheMountStatus`` values) and the
``run_all`` pre-phase: ``mounts.assess_cache_mounts`` runs exactly once per
call, ``check_cache_mounts``'s result comes from that single assessment
(never a second call through the registered check function), and every
cache-touching check is skipped - never invoked - when any status blocks.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import bakar.diagnostics as diagnostics
from bakar.config import BuildConfig
from bakar.diagnostics import (
    CHECK_GROUPS,
    SHARED_CHECKS,
    Severity,
    Status,
    _cache_mounts_result,
    check_cache_mounts,
    run_all,
)
from bakar.mounts import CacheMountStatus

pytestmark = pytest.mark.unit


def _cfg(**over: object) -> BuildConfig:
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
        "sstate_dir": "/cache/sstate",
        "dl_dir": "/cache/downloads",
    }
    base.update(over)
    return BuildConfig(**base)  # type: ignore[arg-type]


_STATUS_DEFAULTS: dict[str, object] = {
    "label": "sstate_dir",
    "path": Path("/cache/sstate"),
    "state": "ready",
    "fstype": "nfs4",
    "mountpoint": "/cache/sstate",
    "source": "server:/exports/sstate",
    "declared_nfs": True,
    "fstab_source": "server:/exports/sstate",
    "detail": "",
}


def _status(**over: object) -> CacheMountStatus:
    fields = {**_STATUS_DEFAULTS, **over}
    return CacheMountStatus(**fields)  # type: ignore[arg-type]


# --- _cache_mounts_result (pure formatting) ---------------------------------


def test_empty_statuses_is_skip() -> None:
    result = _cache_mounts_result([])
    assert result.name == "cache-mounts"
    assert result.status == Status.SKIP


def test_unresponsive_fails_block_naming_path_and_server() -> None:
    status = _status(state="unresponsive", source="nfsbox:/exports/sstate", fstab_source=None)
    result = _cache_mounts_result([status])
    assert result.status == Status.FAIL
    assert result.severity == Severity.BLOCK
    assert str(status.path) in result.message
    assert "nfsbox" in result.message
    assert "did not answer within 20s" in result.message


def test_error_state_reports_the_probe_error_text() -> None:
    status = _status(state="error", detail="stat: cannot read filesystem information")
    result = _cache_mounts_result([status])
    assert result.status == Status.FAIL
    assert "stat: cannot read filesystem information" in result.message


def test_two_unusable_directories_join_into_one_message() -> None:
    a = _status(label="sstate_dir", path=Path("/cache/sstate"), state="unresponsive")
    b = _status(label="dl_dir", path=Path("/cache/downloads"), state="error", detail="boom")
    result = _cache_mounts_result([a, b])
    assert result.status == Status.FAIL
    assert "sstate_dir /cache/sstate" in result.message
    assert "dl_dir /cache/downloads" in result.message
    assert result.message.count(",") >= 1


def test_declared_nfs_but_local_fails_with_fstab_wording() -> None:
    status = _status(state="ready", fstype="ext4", declared_nfs=True, fstab_source="nfsbox:/exports/sstate")
    result = _cache_mounts_result([status])
    assert result.status == Status.FAIL
    assert result.severity == Severity.BLOCK
    assert "declared NFS in /etc/fstab but resolves to ext4" in result.message


def test_declared_nfs_but_local_names_the_fstab_server_not_the_local_device() -> None:
    """The mount table's source is the local device (e.g. /dev/sda1), which
    names no server; the actionable host is the one fstab declared."""
    status = _status(
        state="ready",
        fstype="ext4",
        source="/dev/sda1",
        declared_nfs=True,
        fstab_source="nfsbox:/exports/sstate",
    )
    result = _cache_mounts_result([status])
    assert "nfsbox" in result.message
    assert "/dev/sda1" not in result.message


def test_probe_mounted_nfs_is_pass_never_fail() -> None:
    status = _status(state="ready", fstype="nfs4", declared_nfs=False, fstab_source=None)
    result = _cache_mounts_result([status])
    assert result.status == Status.PASS


def test_healthy_multiple_directories_is_pass() -> None:
    a = _status(label="sstate_dir", path=Path("/cache/sstate"))
    b = _status(label="dl_dir", path=Path("/cache/downloads"))
    result = _cache_mounts_result([a, b])
    assert result.status == Status.PASS
    assert result.severity == Severity.BLOCK


def test_no_mount_or_fstab_source_reports_server_unknown() -> None:
    status = _status(state="unresponsive", source=None, fstab_source=None)
    result = _cache_mounts_result([status])
    assert result.status == Status.FAIL
    assert "server unknown" in result.message


def test_noncritical_unresponsive_target_blocks_like_critical() -> None:
    """A non-critical (ccache) target that is wedged must BLOCK, not WARN.

    The cache-mount-readiness spec's "any effective cache directory" wording
    carries no criticality carve-out: ``critical=False`` no longer downgrades
    a problem to a warning (see ``CacheMountStatus.blocking``).
    """
    status = _status(
        label="ccache_dir",
        path=Path("/cache/ccache"),
        state="unresponsive",
        source="nfsbox:/exports/ccache",
        fstab_source=None,
        critical=False,
    )
    result = _cache_mounts_result([status])
    assert result.status == Status.FAIL
    assert result.severity == Severity.BLOCK
    assert "ccache_dir" in result.message
    assert str(status.path) in result.message
    assert "did not answer within 20s" in result.message


def test_critical_and_noncritical_unresponsive_targets_both_block() -> None:
    """A critical (sstate/downloads) and a non-critical (ccache) target with
    the same problem both block, and both are named in the message."""
    critical_status = _status(
        label="sstate_dir",
        path=Path("/cache/sstate"),
        state="unresponsive",
        critical=True,
    )
    noncritical_status = _status(
        label="ccache_dir",
        path=Path("/cache/ccache"),
        state="unresponsive",
        critical=False,
    )
    result = _cache_mounts_result([critical_status, noncritical_status])
    assert result.status == Status.FAIL
    assert result.severity == Severity.BLOCK
    assert "sstate_dir" in result.message
    assert "ccache_dir" in result.message


# --- registration: SHARED_CHECKS / CHECK_GROUPS / _CLUSTER_CHECKS ----------


def test_cache_mounts_is_first_in_shared_checks() -> None:
    assert SHARED_CHECKS[0] is check_cache_mounts


def test_cache_mounts_is_in_caches_and_storage_group() -> None:
    groups = dict(CHECK_GROUPS)
    assert "cache-mounts" in groups["Caches & storage"]


def test_cache_mounts_not_in_cluster_checks() -> None:
    from bakar.diagnostics import _CLUSTER_CHECKS

    assert check_cache_mounts not in _CLUSTER_CHECKS


def test_check_cache_mounts_thin_wrapper_calls_assessment(monkeypatch: pytest.MonkeyPatch) -> None:
    """The standalone registered function still calls the assessment itself."""
    status = _status()
    calls = []

    def _fake_assess(targets: object, *, deadline_s: float = 20.0) -> list[CacheMountStatus]:
        calls.append(targets)
        return [status]

    monkeypatch.setattr(diagnostics.mounts, "assess_cache_mounts", _fake_assess)
    result = check_cache_mounts(_cfg())
    assert result.status == Status.PASS
    assert len(calls) == 1


# --- run_all pre-phase: single call, gating, never re-calling the check ----


def _install_spy_cache_touching(monkeypatch: pytest.MonkeyPatch, names: list[str]) -> dict[str, int]:
    """Replace every member of _CACHE_TOUCHING_CHECKS with a call-counting spy.

    Also swaps SHARED_CHECKS for a small stand-in list (cache-mounts + the
    named spies) so the test does not have to satisfy every real check's
    config requirements.
    """
    call_counts: dict[str, int] = dict.fromkeys(names, 0)
    spies = []
    name_by_spy: dict[object, str] = {}
    for spy_name in names:

        def _make_spy(n: str) -> object:
            def _spy(_cfg: BuildConfig) -> object:
                call_counts[n] += 1
                return diagnostics._ok(n, Severity.WARN, "ran")

            return _spy

        spy = _make_spy(spy_name)
        spies.append(spy)
        name_by_spy[spy] = spy_name

    # run_all resolves a check's registered name via _CHECK_NAME (used for both
    # the crash-isolation branch and the cache-touching SKIP branch); a spy
    # function has no entry there by default, so its resolved name would fall
    # back to the raw (and identical, since every spy shares the closure's
    # inner function) "_spy" __name__ instead of the intended check name.
    patched_check_name = dict(diagnostics._CHECK_NAME)
    patched_check_name.update(name_by_spy)
    monkeypatch.setattr(diagnostics, "_CHECK_NAME", patched_check_name)
    monkeypatch.setattr(diagnostics, "_CACHE_TOUCHING_CHECKS", tuple(spies))
    monkeypatch.setattr(diagnostics, "SHARED_CHECKS", (check_cache_mounts, *spies))
    return call_counts


def test_run_all_calls_assessment_once_and_skips_cache_touching_checks_when_blocking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call_counts = _install_spy_cache_touching(monkeypatch, ["cache-dirs", "disk-free"])

    assess_calls = []

    def _fake_assess(targets: object, *, deadline_s: float = 20.0) -> list[CacheMountStatus]:
        assess_calls.append(targets)
        if len(assess_calls) > 1:
            raise AssertionError("assess_cache_mounts called more than once per run_all call")
        return [_status(state="unresponsive")]

    monkeypatch.setattr(diagnostics.mounts, "assess_cache_mounts", _fake_assess)

    results = run_all(_cfg())

    assert len(assess_calls) == 1
    cache_mounts_result = next(r for r in results if r.name == "cache-mounts")
    assert cache_mounts_result.status == Status.FAIL
    assert cache_mounts_result.severity == Severity.BLOCK

    for spy_name in call_counts:
        assert call_counts[spy_name] == 0, f"{spy_name} spy was called despite a blocking cache-mounts result"

    skip_results = {r.name: r for r in results if r.name in call_counts}
    assert len(skip_results) == len(call_counts), "expected every cache-touching spy to produce a SKIP result"
    for r in skip_results.values():
        assert r.status == Status.SKIP
        assert "unusable" in r.message
        assert "cache-mounts" in r.message


def test_run_all_joins_two_unusable_directories_into_one_skip_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_spy_cache_touching(monkeypatch, ["cache-dirs"])

    statuses = [
        _status(label="sstate_dir", path=Path("/cache/sstate"), state="unresponsive"),
        _status(label="dl_dir", path=Path("/cache/downloads"), state="error", detail="boom"),
    ]

    def _fake_assess(targets: object, *, deadline_s: float = 20.0) -> list[CacheMountStatus]:
        return statuses

    monkeypatch.setattr(diagnostics.mounts, "assess_cache_mounts", _fake_assess)

    results = run_all(_cfg())
    skip_result = next(r for r in results if r.name == "cache-dirs")
    assert skip_result.status == Status.SKIP
    assert "sstate_dir /cache/sstate" in skip_result.message
    assert "dl_dir /cache/downloads" in skip_result.message


def test_run_all_skips_cache_touching_checks_for_noncritical_unresponsive_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wedged non-critical (ccache) target now BLOCKS in cache-mounts (no
    criticality carve-out), and cache-touching checks must not run against it."""
    call_counts = _install_spy_cache_touching(monkeypatch, ["ccache-health", "cache-dirs"])

    statuses = [
        _status(label="sstate_dir", path=Path("/cache/sstate"), state="ready"),
        _status(label="ccache_dir", path=Path("/cache/ccache"), state="unresponsive", critical=False),
    ]

    def _fake_assess(targets: object, *, deadline_s: float = 20.0) -> list[CacheMountStatus]:
        return statuses

    monkeypatch.setattr(diagnostics.mounts, "assess_cache_mounts", _fake_assess)

    results = run_all(_cfg())

    cache_mounts_result = next(r for r in results if r.name == "cache-mounts")
    assert cache_mounts_result.status == Status.FAIL
    assert cache_mounts_result.severity == Severity.BLOCK

    for spy_name, count in call_counts.items():
        assert count == 0, f"{spy_name} ran against an unresponsive ccache dir"
    for spy_name in call_counts:
        r = next(r for r in results if r.name == spy_name)
        assert r.status == Status.SKIP
        assert "ccache_dir /cache/ccache" in r.message
        assert "sstate_dir" not in r.message


def test_run_all_healthy_calls_every_cache_touching_spy_once(monkeypatch: pytest.MonkeyPatch) -> None:
    call_counts = _install_spy_cache_touching(monkeypatch, ["cache-dirs", "disk-free", "ccache-health"])

    def _fake_assess(targets: object, *, deadline_s: float = 20.0) -> list[CacheMountStatus]:
        return [_status(state="ready")]

    monkeypatch.setattr(diagnostics.mounts, "assess_cache_mounts", _fake_assess)

    results = run_all(_cfg())

    for spy_name, count in call_counts.items():
        assert count == 1, f"{spy_name} spy expected exactly one call, got {count}"
    cache_mounts_result = next(r for r in results if r.name == "cache-mounts")
    assert cache_mounts_result.status == Status.PASS


def test_run_all_calls_assessment_exactly_once_even_though_check_cache_mounts_is_in_shared_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No monkeypatching of SHARED_CHECKS here - locks the real, un-patched list."""
    calls = []

    def _fake_assess(targets: object, *, deadline_s: float = 20.0) -> list[CacheMountStatus]:
        calls.append(targets)
        if len(calls) > 1:
            raise AssertionError("assess_cache_mounts called more than once per run_all call")
        return [_status(state="ready")]

    monkeypatch.setattr(diagnostics.mounts, "assess_cache_mounts", _fake_assess)
    results = run_all(_cfg())

    assert len(calls) == 1
    assert any(r.name == "cache-mounts" for r in results)
