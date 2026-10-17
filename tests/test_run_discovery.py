"""Unit tests for :mod:`bakar.run_discovery`.

Covers workspace-wide run enumeration, the per-candidate liveness check, the
host-mode correlation of ``/proc`` discoveries, and the host/container dedup.
Every test is hermetic: the stop module's liveness and gate seams, and the
container runtime query, are patched rather than executed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from bakar import build_stop, run_discovery, stop_container

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from bakar.config import BuildConfig

pytestmark = pytest.mark.unit


def _make_run_dir(bsp_root: Path, run_id: str = "20260618-120000") -> Path:
    """Create ``bsp_root/build/runs/<run_id>`` and return it."""
    run_dir = bsp_root / "build" / "runs" / run_id
    run_dir.mkdir(parents=True)
    return run_dir


# --- correlate_host_discoveries: group 7 discoveries x group 2 enumeration -


def test_correlate_host_discoveries_matches_live_run_under_topdir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    nfs_probe: Callable[[bool | None], None],
) -> None:
    """A discovered topdir with a live cooker resolves to its live run candidate
    via live_workspace_runs(topdir / "runs") - not a second launch-record reader."""
    nfs_probe(False)
    run_dir = _make_run_dir(tmp_path / "nxp", "20260618-120000-111")
    build_stop.write_launch_record(run_dir, pgid=4242, mode="host")
    topdir = run_dir.parent.parent  # tmp_path/nxp/build

    monkeypatch.setattr(build_stop, "is_build_running", lambda _rd: (True, 4242, True))

    candidates = run_discovery.correlate_host_discoveries({topdir: frozenset({4242})})

    assert [c.run_dir for c in candidates] == [run_dir]


def test_correlate_host_discoveries_drops_topdir_with_no_matching_live_run(
    tmp_path: Path,
    nfs_probe: Callable[[bool | None], None],
) -> None:
    """A topdir with a live cooker but no run directory underneath it (launch-record
    write race, or a cooker started outside bakar's own entry point) is dropped
    silently - not reported with a missing or fabricated run id."""
    nfs_probe(False)
    topdir = tmp_path / "orphan" / "build"  # no runs/ subdirectory exists at all

    candidates = run_discovery.correlate_host_discoveries({topdir: frozenset({7777})})

    assert candidates == []


def test_correlate_host_discoveries_multiple_topdirs_each_correlate_independently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    nfs_probe: Callable[[bool | None], None],
) -> None:
    """Two discovered topdirs on the same host each resolve to their own live run,
    and a dead run under one of them never leaks into the result."""
    nfs_probe(False)

    nxp_run = _make_run_dir(tmp_path / "nxp", "20260618-120000-111")
    build_stop.write_launch_record(nxp_run, pgid=4242, mode="host")
    nxp_dead_run = _make_run_dir(tmp_path / "nxp", "20260618-100000-000")
    build_stop.write_launch_record(nxp_dead_run, pgid=9999, mode="host")

    ti_run = _make_run_dir(tmp_path / "ti", "20260618-130000-222")
    build_stop.write_launch_record(ti_run, pgid=5252, mode="host")

    def fake_is_build_running(run_dir: Path) -> tuple[bool, int | None, bool]:
        if run_dir in (nxp_run, ti_run):
            return (True, 1, True)
        return (False, None, False)

    monkeypatch.setattr(build_stop, "is_build_running", fake_is_build_running)

    discovered = {
        nxp_run.parent.parent: frozenset({4242}),
        ti_run.parent.parent: frozenset({5252}),
    }
    candidates = run_discovery.correlate_host_discoveries(discovered)

    assert {c.run_dir for c in candidates} == {nxp_run, ti_run}


def test_correlate_host_discoveries_multiple_worker_pids_collapse_to_one_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    nfs_probe: Callable[[bool | None], None],
) -> None:
    """Confirming test for group 12's host-mode dedup: when the cooker has spawned
    several bitbake-worker processes that also match group 7's argv scan under the
    same topdir, correlate_host_discoveries still produces exactly one candidate
    for that run id - not one per matched PID. The frozenset of matched PIDs is
    never iterated for row content; the single candidate's metadata comes from
    the run directory's own launch record (the cooker), reached once per topdir
    via live_workspace_runs, regardless of how many PIDs matched there."""
    nfs_probe(False)
    run_dir = _make_run_dir(tmp_path / "nxp", "20260618-120000-111")
    build_stop.write_launch_record(run_dir, pgid=4242, mode="host")
    topdir = run_dir.parent.parent  # tmp_path/nxp/build

    monkeypatch.setattr(build_stop, "is_build_running", lambda _rd: (True, 4242, True))

    # Cooker PID 4242 plus several bitbake-worker PIDs all matched under this
    # same topdir by _discover_host_cookers's argv scan.
    discovered = {topdir: frozenset({4242, 4300, 4301, 4302})}

    candidates = run_discovery.correlate_host_discoveries(discovered)

    assert len(candidates) == 1
    assert candidates[0].run_dir == run_dir


# --- dedup_across_sources (group 13: cross-source dedup, host vs container) --


def test_dedup_across_sources_drops_host_row_matching_container_run_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    nfs_probe: Callable[[bool | None], None],
) -> None:
    """A run id reported by both host-mode correlation and container-mode discovery
    keeps only the container-mode row; the duplicate host row is dropped."""
    nfs_probe(False)
    run_dir = _make_run_dir(tmp_path / "nxp", "20260618-120000-111")
    build_stop.write_launch_record(run_dir, pgid=4242, mode="host")
    topdir = run_dir.parent.parent
    monkeypatch.setattr(build_stop, "is_build_running", lambda _rd: (True, 4242, True))
    host_candidates = run_discovery.correlate_host_discoveries({topdir: frozenset({4242})})
    assert len(host_candidates) == 1  # sanity: the collision run id is present

    container_candidates = [stop_container.ContainerCandidate(run_id="20260618-120000-111", container_id="cid-main")]

    deduped_host, deduped_container = run_discovery.dedup_across_sources(host_candidates, container_candidates)

    assert deduped_host == []
    assert deduped_container == container_candidates


def test_dedup_across_sources_keeps_host_row_with_no_container_match(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    nfs_probe: Callable[[bool | None], None],
) -> None:
    """A host-mode run id with no matching container-mode row is kept unchanged."""
    nfs_probe(False)
    run_dir = _make_run_dir(tmp_path / "nxp", "20260618-120000-111")
    build_stop.write_launch_record(run_dir, pgid=4242, mode="host")
    topdir = run_dir.parent.parent
    monkeypatch.setattr(build_stop, "is_build_running", lambda _rd: (True, 4242, True))
    host_candidates = run_discovery.correlate_host_discoveries({topdir: frozenset({4242})})

    container_candidates = [stop_container.ContainerCandidate(run_id="20260618-130000-222", container_id="cid-other")]

    deduped_host, deduped_container = run_discovery.dedup_across_sources(host_candidates, container_candidates)

    assert deduped_host == host_candidates
    assert deduped_container == container_candidates


def test_dedup_across_sources_container_candidates_never_dropped() -> None:
    """Container candidates are returned unchanged regardless of host-side collisions -
    only host candidates are ever removed by this function."""
    container_candidates = [
        stop_container.ContainerCandidate(run_id="20260618-120000-111", container_id="cid-main"),
        stop_container.ContainerCandidate(run_id="20260618-130000-222", container_id="cid-other"),
    ]

    deduped_host, deduped_container = run_discovery.dedup_across_sources([], container_candidates)

    assert deduped_host == []
    assert deduped_container == container_candidates


def test_dedup_across_sources_empty_inputs_are_empty() -> None:
    assert run_discovery.dedup_across_sources([], []) == ([], [])


# --- is_candidate_live: standalone per-candidate liveness check -------------


def test_is_candidate_live_host_mode_alive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nfs_probe: Callable[[bool | None], None]
) -> None:
    """A host-mode candidate is live when is_build_running reports alive+cmdline_ok."""
    nfs_probe(False)
    ws = tmp_path
    run_dir = _make_run_dir(ws / "nxp", "20260618-120000-111")
    build_stop.write_launch_record(run_dir, pgid=4242, mode="host")
    monkeypatch.setattr(build_stop, "is_build_running", lambda _rd: (True, 4242, True))

    scan = run_discovery.enumerate_workspace_runs(ws)
    (candidate,) = scan.candidates

    assert run_discovery.is_candidate_live(candidate) is True


def test_is_candidate_live_host_mode_dead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nfs_probe: Callable[[bool | None], None]
) -> None:
    """A host-mode candidate is not live when is_build_running reports dead."""
    nfs_probe(False)
    ws = tmp_path
    run_dir = _make_run_dir(ws / "nxp", "20260618-120000-111")
    build_stop.write_launch_record(run_dir, pgid=9999, mode="host")
    monkeypatch.setattr(build_stop, "is_build_running", lambda _rd: (False, None, False))

    scan = run_discovery.enumerate_workspace_runs(ws)
    (candidate,) = scan.candidates

    assert run_discovery.is_candidate_live(candidate) is False


def test_is_candidate_live_container_mode_alive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nfs_probe: Callable[[bool | None], None]
) -> None:
    """A container-mode candidate is live when the runtime confirms a match."""
    nfs_probe(False)
    ws = tmp_path
    run_dir = _make_run_dir(ws / "ti", "20260618-130000-222")
    build_stop.write_launch_record(
        run_dir, pgid=5252, mode="container", runtime="docker", container_label="bakar.run_id=ti-run"
    )
    monkeypatch.setattr(stop_container, "_container_id_status", lambda _runtime, _label: (stop_container._ALIVE, "cid"))

    scan = run_discovery.enumerate_workspace_runs(ws)
    (candidate,) = scan.candidates

    assert run_discovery.is_candidate_live(candidate) is True


def test_is_candidate_live_container_mode_dead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nfs_probe: Callable[[bool | None], None]
) -> None:
    """A container-mode candidate is not live once the runtime confirms _DEAD."""
    nfs_probe(False)
    ws = tmp_path
    run_dir = _make_run_dir(ws / "ti", "20260618-130000-222")
    build_stop.write_launch_record(
        run_dir, pgid=5252, mode="container", runtime="docker", container_label="bakar.run_id=ti-run"
    )
    monkeypatch.setattr(stop_container, "_container_id_status", lambda _runtime, _label: (stop_container._DEAD, None))

    scan = run_discovery.enumerate_workspace_runs(ws)
    (candidate,) = scan.candidates

    assert run_discovery.is_candidate_live(candidate) is False


def test_is_candidate_live_container_mode_error_fails_conservative(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    nfs_probe: Callable[[bool | None], None],
) -> None:
    """An unanswerable runtime query (_ERROR) is not treated as dead."""
    nfs_probe(False)
    ws = tmp_path
    run_dir = _make_run_dir(ws / "ti", "20260618-130000-222")
    build_stop.write_launch_record(
        run_dir, pgid=5252, mode="container", runtime="docker", container_label="bakar.run_id=ti-run"
    )
    monkeypatch.setattr(stop_container, "_container_id_status", lambda _runtime, _label: (stop_container._ERROR, None))

    scan = run_discovery.enumerate_workspace_runs(ws)
    (candidate,) = scan.candidates

    assert run_discovery.is_candidate_live(candidate) is True


def test_is_candidate_live_no_label_no_host_mode_is_false(
    tmp_path: Path, nfs_probe: Callable[[bool | None], None]
) -> None:
    """A malformed/missing launch record (no container_label, not host mode) is not live."""
    nfs_probe(False)
    ws = tmp_path
    run_dir = _make_run_dir(ws / "nxp", "20260618-170000-666")
    (run_dir / "build.meta.json").write_text("{not json")
    (run_dir / "build.pid").write_text("not-an-int\n")

    scan = run_discovery.enumerate_workspace_runs(ws)
    (candidate,) = scan.candidates

    assert run_discovery.is_candidate_live(candidate) is False


# --- workspace-wide live-build discovery (task 2.1) -------------------------


def test_workspace_wide_discovery_finds_live_builds_across_family_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    nfs_probe: Callable[[bool | None], None],
) -> None:
    """A live build under a different family root than the one the CLI args
    pointed at (e.g. a live TI build while ``--manifest`` selected NXP) must
    show up in the workspace-wide scan, not just the one root a single-root
    caller happened to resolve. A finished run under the SAME root as a live
    one must not leak into the live-only result either.
    """
    nfs_probe(False)
    ws = tmp_path

    nxp_run = _make_run_dir(ws / "nxp", "20260618-120000-111")
    build_stop.write_launch_record(nxp_run, pgid=4242, mode="host")

    nxp_dead_run = _make_run_dir(ws / "nxp", "20260618-100000-000")
    build_stop.write_launch_record(nxp_dead_run, pgid=9999, mode="host")

    ti_run = _make_run_dir(ws / "ti", "20260618-130000-222")
    build_stop.write_launch_record(
        ti_run, pgid=5252, mode="container", runtime="docker", container_label="bakar.run_id=ti-run"
    )

    def fake_is_build_running(run_dir: Path) -> tuple[bool, int | None, bool]:
        if run_dir == nxp_run:
            return (True, 4242, True)
        return (False, None, False)

    monkeypatch.setattr(build_stop, "is_build_running", fake_is_build_running)
    # Container-mode liveness is now runtime-verified, not just "a launch
    # record with a label exists" - simulate the ti_run's container as
    # genuinely still running.
    monkeypatch.setattr(
        stop_container, "_container_id_status", lambda _runtime, _label: (stop_container._ALIVE, "cid-ti-run")
    )

    live = run_discovery.live_workspace_runs(ws)

    live_by_dir = {c.run_dir: c for c in live}
    assert set(live_by_dir) == {nxp_run, ti_run}
    assert live_by_dir[nxp_run].root.family == "nxp"
    assert live_by_dir[ti_run].root.family == "ti"


def test_enumerate_workspace_runs_covers_plain_and_build_star_roots(
    tmp_path: Path,
    nfs_probe: Callable[[bool | None], None],
) -> None:
    """The plain ``<ws>/build/runs`` root and every ``<ws>/build-*/build/runs``
    root are scanned too, not just nxp/ti."""
    nfs_probe(False)
    ws = tmp_path

    plain_run = _make_run_dir(ws, "20260618-140000-333")
    build_stop.write_launch_record(plain_run, pgid=1, mode="host")

    fanout_run = _make_run_dir(ws / "build-qemux86-64", "20260618-150000-444")
    build_stop.write_launch_record(fanout_run, pgid=1, mode="host")

    scan = run_discovery.enumerate_workspace_runs(ws)

    found = {c.run_dir: c.root.family for c in scan.candidates}
    assert found == {plain_run: "generic", fanout_run: "generic"}


def test_enumerate_workspace_runs_callable_against_bare_runs_dir(
    tmp_path: Path,
    nfs_probe: Callable[[bool | None], None],
) -> None:
    """Passing a bare ``.../build/runs`` path scans that one root directly,
    without probing for nxp/ti/build-* subdirectories - the shape group 9's
    future host-mode correlation needs (a discovered topdir, not a workspace)."""
    nfs_probe(False)
    topdir = tmp_path / "some" / "topdir"
    run_dir = _make_run_dir(topdir, "20260618-160000-555")
    build_stop.write_launch_record(run_dir, pgid=1, mode="host")

    scan = run_discovery.enumerate_workspace_runs(run_dir.parent)

    assert [c.run_dir for c in scan.candidates] == [run_dir]
    assert scan.candidates[0].root.bsp_root == topdir


def test_enumerate_workspace_runs_bare_runs_dir_preserves_nxp_ti_family(
    tmp_path: Path,
    nfs_probe: Callable[[bool | None], None],
) -> None:
    """A bare ``.../nxp/build/runs`` (or ``.../ti/build/runs``) path - the shape
    a host-mode /proc scan discovers with no workspace in hand - resolves to
    its real family and to the workspace ABOVE the family root, not to a
    generic/bbsetup placeholder. Host-mode discovery is the only production
    path that reaches this branch (``correlate_host_discoveries`` always
    calls it via ``topdir / "runs"``), so a wrong family here means every
    ``bakar ps`` row for an nxp/ti host-mode build reports the wrong family
    and resolves machine as if the build were bbsetup."""
    nfs_probe(False)

    nxp_run = _make_run_dir(tmp_path / "nxp", "20260618-170000-666")
    build_stop.write_launch_record(nxp_run, pgid=1, mode="host")
    ti_run = _make_run_dir(tmp_path / "ti", "20260618-170000-777")
    build_stop.write_launch_record(ti_run, pgid=1, mode="host")

    nxp_scan = run_discovery.enumerate_workspace_runs(nxp_run.parent)
    ti_scan = run_discovery.enumerate_workspace_runs(ti_run.parent)

    assert [c.run_dir for c in nxp_scan.candidates] == [nxp_run]
    assert nxp_scan.candidates[0].root.family == "nxp"
    assert nxp_scan.candidates[0].root.resolve_family == "nxp"
    assert nxp_scan.candidates[0].root.resolve_workspace == tmp_path

    assert [c.run_dir for c in ti_scan.candidates] == [ti_run]
    assert ti_scan.candidates[0].root.family == "ti"
    assert ti_scan.candidates[0].root.resolve_family == "ti"
    assert ti_scan.candidates[0].root.resolve_workspace == tmp_path


def test_enumerate_workspace_runs_malformed_launch_record_excluded_from_live(
    tmp_path: Path,
    nfs_probe: Callable[[bool | None], None],
) -> None:
    """A malformed/unreadable launch record excludes that candidate from the
    live-only result rather than raising; it still appears in the unfiltered
    enumeration."""
    nfs_probe(False)
    ws = tmp_path
    run_dir = _make_run_dir(ws / "nxp", "20260618-170000-666")
    (run_dir / "build.meta.json").write_text("{not json")
    (run_dir / "build.pid").write_text("not-an-int\n")

    scan = run_discovery.enumerate_workspace_runs(ws)
    assert [c.run_dir for c in scan.candidates] == [run_dir]

    live = run_discovery.live_workspace_runs(ws)
    assert live == []


def test_enumerate_workspace_runs_gate_refusal_reports_skipped_root_not_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A root the ownership gate refuses is reported as skipped with the
    refusal reason - never silently treated as 'nothing running there'."""
    ws = tmp_path
    (ws / "nxp").mkdir(parents=True)
    run_dir = _make_run_dir(ws / "nxp", "20260618-180000-777")
    build_stop.write_launch_record(run_dir, pgid=1, mode="host")

    def fake_guard(cfg: BuildConfig) -> build_stop.LockRefusal | None:
        if cfg.bsp_root == ws / "nxp":
            return build_stop.LockRefusal(reason="peer-held", host="peer-host")
        return None

    monkeypatch.setattr(build_stop, "lock_mutation_guard", fake_guard)

    scan = run_discovery.enumerate_workspace_runs(ws)

    assert run_dir not in [c.run_dir for c in scan.candidates]
    skipped_roots = {s.root.bsp_root: s.refusal.reason for s in scan.skipped}
    assert skipped_roots[ws / "nxp"] == "peer-held"


def test_enumerate_workspace_runs_resolve_failure_excludes_root_without_crashing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    nfs_probe: Callable[[bool | None], None],
) -> None:
    """A root whose config resolution raises is excluded from the scan
    entirely rather than crashing the whole workspace-wide discovery, and
    the drop is logged rather than fully silent - it is not recorded in
    ``scan.skipped``, which is reserved for the lock-ownership gate's own
    refusal reasons, a different failure class."""
    nfs_probe(False)
    ws = tmp_path
    nxp_run = _make_run_dir(ws / "nxp", "20260618-190000-888")
    build_stop.write_launch_record(nxp_run, pgid=1, mode="host")
    ti_run = _make_run_dir(ws / "ti", "20260618-200000-999")
    build_stop.write_launch_record(ti_run, pgid=1, mode="host")

    real_resolve = run_discovery.resolve

    def flaky_resolve(request: run_discovery.ResolveRequest) -> BuildConfig:
        if request.bsp_family == "ti":
            raise ValueError("boom")
        return real_resolve(request)

    monkeypatch.setattr(run_discovery, "resolve", flaky_resolve)

    with caplog.at_level("WARNING", logger="bakar.run_discovery"):
        scan = run_discovery.enumerate_workspace_runs(ws)

    assert [c.run_dir for c in scan.candidates] == [nxp_run]
    assert scan.skipped == []
    assert any(str(ws / "ti") in record.message and "boom" in record.message for record in caplog.records)


def test_live_workspace_runs_timing_20_candidates_across_4_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    nfs_probe: Callable[[bool | None], None],
) -> None:
    """The live-only filter completes well under a second against a fixture
    with >=20 candidate run directories spread across the 4 roots - per-root
    config resolution (a handful of calls) must not become per-run."""
    import time as time_module

    nfs_probe(False)
    ws = tmp_path
    roots = [ws / "nxp", ws / "ti", ws, ws / "build-qemux86-64"]
    all_run_dirs: list[Path] = []
    for root_index, root in enumerate(roots):
        for i in range(5):
            run_dir = _make_run_dir(root, f"2026061{root_index}-{i:02d}0000-{root_index}{i:02d}")
            build_stop.write_launch_record(run_dir, pgid=1, mode="host")
            all_run_dirs.append(run_dir)
    assert len(all_run_dirs) >= 20

    monkeypatch.setattr(build_stop, "is_build_running", lambda _rd: (False, None, False))

    start = time_module.monotonic()
    live = run_discovery.live_workspace_runs(ws)
    elapsed = time_module.monotonic() - start

    assert live == []
    assert elapsed < 1.0, f"live_workspace_runs took {elapsed:.3f}s for 20 candidates across 4 roots"
