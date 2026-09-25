"""Per-check deadlines and ``on_check`` start/end instrumentation in ``run_all``.

Covers task 4.1 (spec: doctor-check-crash-isolation): a check that hangs past
``_CHECK_DEADLINE_SECONDS`` fails under its registered name and ceiling
severity instead of hanging the whole doctor run, the ``on_check`` callback
receives ordered start/end events built from the one ``CheckResult`` that
lands in ``results`` (never a distinct "timeout" status literal), and the
``check_cache_mounts`` pre-phase timing is measured once and is distinct from
a gated SKIP's zero-second event.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import bakar.diagnostics as diagnostics
from bakar.config import BuildConfig
from bakar.diagnostics import (
    CheckEvent,
    CheckResult,
    Severity,
    Status,
    any_blocking_failure,
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


def _blocked_check(cfg: BuildConfig) -> CheckResult:
    threading.Event().wait()  # never set by anyone: blocks forever
    return CheckResult(name="blocked-check", severity=Severity.BLOCK, status=Status.PASS, message="unreachable")


def _ok_check(cfg: BuildConfig) -> CheckResult:
    return CheckResult(name="ok-check", severity=Severity.INFO, status=Status.PASS, message="fine")


def _install_blocked_and_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(diagnostics, "SHARED_CHECKS", (_blocked_check, _ok_check))
    monkeypatch.setattr(
        diagnostics,
        "_CHECK_NAME",
        {_blocked_check: "blocked-check", _ok_check: "ok-check"},
    )
    monkeypatch.setattr(
        diagnostics,
        "_CHECK_SEVERITY",
        {"blocked-check": Severity.BLOCK, "ok-check": Severity.INFO},
    )
    monkeypatch.setattr(diagnostics, "_DOCKER_CHECKS", ())
    monkeypatch.setattr(diagnostics, "_CLUSTER_CHECKS", ())
    monkeypatch.setattr(diagnostics, "_POST_BUILD_CHECKS", ())
    monkeypatch.setattr(diagnostics, "_CHECK_DEADLINE_SECONDS", 0.2)


def test_timeout_yields_fail_under_registered_name_and_next_check_still_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_blocked_and_ok(monkeypatch)

    start = time.monotonic()
    results = run_all(_cfg())
    elapsed = time.monotonic() - start

    assert elapsed < 1.0, f"run_all took {elapsed:.2f}s, should fail the blocked check within its 0.2s deadline"
    by_name = {r.name: r for r in results}
    assert by_name["blocked-check"].status == Status.FAIL
    assert "did not finish within" in by_name["blocked-check"].message
    assert "ok-check" in by_name, "the next check after a timed-out one must still run"
    assert by_name["ok-check"].status == Status.PASS


def test_block_ceiling_timeout_sets_any_blocking_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_blocked_and_ok(monkeypatch)

    results = run_all(_cfg())

    assert any_blocking_failure(results)


def test_on_check_events_ordered_and_timeout_end_matches_appended_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_blocked_and_ok(monkeypatch)

    events: list[CheckEvent] = []
    results = run_all(_cfg(), on_check=events.append)
    by_name = {r.name: r for r in results}

    assert [e.phase for e in events] == ["start", "end", "start", "end"]
    assert [e.name for e in events] == [
        "blocked-check",
        "blocked-check",
        "ok-check",
        "ok-check",
    ]

    blocked_end = events[1]
    blocked_result = by_name["blocked-check"]
    # No distinct "timeout" status literal: the end event is built from the
    # same CheckResult that got appended, so it carries the ordinary FAIL
    # status and string forms of the plain Status/Severity enums.
    assert blocked_end.status == blocked_result.status.value
    assert isinstance(blocked_end.status, str)
    assert blocked_end.severity == blocked_result.severity.value
    assert blocked_end.severity == Severity.BLOCK.value

    ok_end = events[3]
    ok_result = by_name["ok-check"]
    assert ok_end.status == ok_result.status.value
    assert ok_end.severity == ok_result.severity.value


def test_cache_mounts_timing_measured_once_and_distinct_from_gated_skip_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``check_cache_mounts``'s on_check pair times the pre-phase probe call;
    a check gated out by a blocking cache status gets seconds == 0.0 because
    no thread ever ran for it."""

    def _spy_cache_touching(_cfg: BuildConfig) -> CheckResult:
        return diagnostics._ok("cache-touching-spy", Severity.WARN, "ran")

    monkeypatch.setattr(diagnostics, "SHARED_CHECKS", (check_cache_mounts, _spy_cache_touching))
    monkeypatch.setattr(
        diagnostics,
        "_CHECK_NAME",
        {check_cache_mounts: "cache-mounts", _spy_cache_touching: "cache-touching-spy"},
    )
    monkeypatch.setattr(diagnostics, "_CHECK_SEVERITY", {"cache-touching-spy": Severity.WARN})
    monkeypatch.setattr(diagnostics, "_CACHE_TOUCHING_CHECKS", (_spy_cache_touching,))
    monkeypatch.setattr(diagnostics, "_DOCKER_CHECKS", ())
    monkeypatch.setattr(diagnostics, "_CLUSTER_CHECKS", ())
    monkeypatch.setattr(diagnostics, "_POST_BUILD_CHECKS", ())

    blocking_status = CacheMountStatus(
        label="sstate_dir",
        path=Path("/cache/sstate"),
        state="unresponsive",
        fstype=None,
        mountpoint=None,
        source=None,
        declared_nfs=True,
        fstab_source="nfsbox:/exports/sstate",
        detail="timed out",
    )

    def _fake_assess(targets: object, *, deadline_s: float = 20.0) -> list[CacheMountStatus]:
        time.sleep(0.05)
        return [blocking_status]

    monkeypatch.setattr(diagnostics.mounts, "assess_cache_mounts", _fake_assess)

    events: list[CheckEvent] = []
    run_all(_cfg(), on_check=events.append)

    by_name_phase = {(e.name, e.phase): e for e in events}
    cache_mounts_end = by_name_phase[("cache-mounts", "end")]
    skip_end = by_name_phase[("cache-touching-spy", "end")]

    assert cache_mounts_end.seconds > 0
    assert skip_end.seconds == 0.0
    assert cache_mounts_end.seconds != skip_end.seconds


def test_subprocess_run_all_exits_within_deadline_on_stuck_check() -> None:
    """A real subprocess exercising run_all against a check that never
    returns must still exit promptly rather than hanging the process."""
    script = """
import threading
from pathlib import Path

import bakar.diagnostics as diagnostics
from bakar.config import BuildConfig


def blocked_check(cfg):
    threading.Event().wait()


diagnostics.SHARED_CHECKS = (blocked_check,)
diagnostics._CHECK_DEADLINE_SECONDS = 0.2
diagnostics._DOCKER_CHECKS = ()
diagnostics._CLUSTER_CHECKS = ()
diagnostics._POST_BUILD_CHECKS = ()

cfg = BuildConfig(
    workspace=Path("/tmp"),
    bsp_family="nxp",
    machine="m",
    distro="d",
    image="i",
    manifest="x.xml",
    repo_url="https://example.com",
    repo_branch="main",
    kas_container_image="img:latest",
    sstate_dir="/cache/sstate",
    dl_dir="/cache/downloads",
)

results = diagnostics.run_all(cfg, None)
assert any(r.status.value == "FAIL" for r in results), results
print("OK")
"""
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert "OK" in proc.stdout


def test_cache_mounts_pre_phase_timeout_fails_block_and_skips_cache_touching_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cache-mounts pre-phase now runs under the same per-check deadline
    as every other check (doctor-check-crash-isolation spec: "Each pre-flight
    doctor check SHALL run under an individual deadline of 180 seconds", with
    no stated exception for cache-mounts). Before this fix only
    ``probe_statfs``'s own internal 20s bound covered the probe itself; the
    surrounding pre-phase call had no deadline wrapper at all and could hang
    the whole doctor run. A probe that never returns must fail cache-mounts
    at BLOCK severity under its own registered name, and every cache-touching
    check must SKIP rather than run against a cache directory the pre-phase
    never got to classify."""

    def _spy_cache_touching(_cfg: BuildConfig) -> CheckResult:
        return diagnostics._ok("cache-touching-spy", Severity.WARN, "ran")

    monkeypatch.setattr(diagnostics, "SHARED_CHECKS", (check_cache_mounts, _spy_cache_touching))
    monkeypatch.setattr(
        diagnostics,
        "_CHECK_NAME",
        {check_cache_mounts: "cache-mounts", _spy_cache_touching: "cache-touching-spy"},
    )
    monkeypatch.setattr(
        diagnostics,
        "_CHECK_SEVERITY",
        {"cache-mounts": Severity.BLOCK, "cache-touching-spy": Severity.WARN},
    )
    monkeypatch.setattr(diagnostics, "_CACHE_TOUCHING_CHECKS", (_spy_cache_touching,))
    monkeypatch.setattr(diagnostics, "_DOCKER_CHECKS", ())
    monkeypatch.setattr(diagnostics, "_CLUSTER_CHECKS", ())
    monkeypatch.setattr(diagnostics, "_POST_BUILD_CHECKS", ())
    monkeypatch.setattr(diagnostics, "_CHECK_DEADLINE_SECONDS", 0.2)

    def _hung_assess(targets: object, *, deadline_s: float = 20.0) -> list[CacheMountStatus]:
        threading.Event().wait()  # never set by anyone: blocks forever
        return []  # unreachable

    monkeypatch.setattr(diagnostics.mounts, "assess_cache_mounts", _hung_assess)

    start = time.monotonic()
    results = run_all(_cfg())
    elapsed = time.monotonic() - start

    assert elapsed < 2.0, f"run_all took {elapsed:.2f}s, should fail within the 0.2s deadline"
    by_name = {r.name: r for r in results}

    cache_mounts_result = by_name["cache-mounts"]
    assert cache_mounts_result.status == Status.FAIL
    assert cache_mounts_result.severity == Severity.BLOCK
    assert "did not finish within" in cache_mounts_result.message

    spy_result = by_name["cache-touching-spy"]
    assert spy_result.status == Status.SKIP
    assert "cache-mounts" in spy_result.message


def test_cache_mounts_pre_phase_crash_fails_block_and_skips_cache_touching_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bug's twin to the timeout test above: an exception raised inside
    ``assess_cache_mounts`` must not die silently on the pre-phase's own
    thread. Before this fix the probe thread had no try/except of its own -
    unlike the main per-check loop's ``_run`` closure - so a crash there left
    ``cache_statuses`` as ``None`` with no other signal, fell through to
    ``_cache_mounts_result(None)``, and produced a clean-looking SKIP ("no
    effective cache directories configured") instead of a loud failure. That
    let every cache-touching check run UNGATED against directories whose
    health was never actually determined - a false all-clear. A crash must
    fail cache-mounts at BLOCK severity under its own registered name (naming
    the exception, matching the main loop's ``check crashed: {exc!r}``
    shape), and every cache-touching check must SKIP rather than run."""

    def _spy_cache_touching(_cfg: BuildConfig) -> CheckResult:
        return diagnostics._ok("cache-touching-spy", Severity.WARN, "ran")

    monkeypatch.setattr(diagnostics, "SHARED_CHECKS", (check_cache_mounts, _spy_cache_touching))
    monkeypatch.setattr(
        diagnostics,
        "_CHECK_NAME",
        {check_cache_mounts: "cache-mounts", _spy_cache_touching: "cache-touching-spy"},
    )
    monkeypatch.setattr(
        diagnostics,
        "_CHECK_SEVERITY",
        {"cache-mounts": Severity.BLOCK, "cache-touching-spy": Severity.WARN},
    )
    monkeypatch.setattr(diagnostics, "_CACHE_TOUCHING_CHECKS", (_spy_cache_touching,))
    monkeypatch.setattr(diagnostics, "_DOCKER_CHECKS", ())
    monkeypatch.setattr(diagnostics, "_CLUSTER_CHECKS", ())
    monkeypatch.setattr(diagnostics, "_POST_BUILD_CHECKS", ())
    monkeypatch.setattr(diagnostics, "_CHECK_DEADLINE_SECONDS", 0.2)

    def _crashing_assess(targets: object, *, deadline_s: float = 20.0) -> list[CacheMountStatus]:
        raise RuntimeError("bad /proc/mounts line")

    monkeypatch.setattr(diagnostics.mounts, "assess_cache_mounts", _crashing_assess)

    results = run_all(_cfg())
    by_name = {r.name: r for r in results}

    cache_mounts_result = by_name["cache-mounts"]
    assert cache_mounts_result.status == Status.FAIL
    assert cache_mounts_result.severity == Severity.BLOCK
    assert "check crashed" in cache_mounts_result.message
    assert "bad /proc/mounts line" in cache_mounts_result.message

    spy_result = by_name["cache-touching-spy"]
    assert spy_result.status == Status.SKIP
    assert "cache-mounts" in spy_result.message
