"""Workspace-wide run discovery.

Finds the build runs under a workspace and decides which are live. It imports
``bakar.build_stop`` and ``bakar.stop_container`` as modules, never by
from-import, so that patches of the stop module's names keep intercepting
discovery at call time.

``bakar stop`` (and ``bakar ps``) needs to see every family root in a
workspace, not just the one CLI args happened to resolve. This module defines
its own literal root list directly - it does NOT import anything from
``bakar.commands.*`` - and resolves a per-root
:class:`~bakar.config.BuildConfig` directly via :func:`bakar.config.resolve`,
which lives in the project's core configuration module rather than the CLI
command layer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from bakar import build_stop, stop_container
from bakar.config import ResolveRequest, resolve

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Literal

    from bakar.config import BuildConfig
    from bakar.stop_container import ContainerCandidate
    from bakar.user_config import UserConfig

_logger = logging.getLogger("bakar.run_discovery")


@dataclass(frozen=True)
class RunRoot:
    """One family/build root scanned for workspace-wide live-build discovery.

    ``bsp_root`` is where ``build/runs`` actually lives on disk.
    ``resolve_workspace``/``resolve_family`` are the two :func:`resolve`
    inputs that reproduce it: the nxp/ti roots pass the top-level workspace
    with their own family (``resolve()`` computes ``bsp_root = workspace /
    bsp_family`` for those), while the plain ``build/runs`` root and every
    ``build-*`` root pass the root itself with ``bsp_family="bbsetup"``,
    whose :attr:`BuildConfig.bsp_root` is unconditionally its own
    ``workspace`` input - mirroring
    ``resolve_run_config`` in ``commands/_helpers.py`` (not reused directly,
    because this module must not import ``bakar.commands.*``).

    A caller with just one already-known root - group 9's future host-mode
    correlation, scanning a topdir discovered via a ``/proc`` walk with no
    workspace in hand - builds a single ``RunRoot`` the same way instead of
    going through :func:`_workspace_roots`.
    """

    bsp_root: Path
    family: Literal["nxp", "ti", "generic"]
    resolve_workspace: Path
    resolve_family: Literal["nxp", "ti", "bbsetup"]

    @property
    def runs_dir(self) -> Path:
        """``bsp_root/build/runs``.

        Always literally ``"build"``, never ``build_dir_name`` off the
        resolved config: ``resolve_family`` is never ``"qcom"`` here (the only
        family whose ``build_dir_name`` differs), so the two agree by
        construction.
        """
        return self.bsp_root / "build" / "runs"


@dataclass(frozen=True)
class SkippedRoot:
    """A candidate root excluded from discovery by the NFS lock-ownership gate."""

    root: RunRoot
    refusal: build_stop.LockRefusal


@dataclass(frozen=True)
class RunCandidate:
    """One run directory discovered under a scanned root, with its root's config."""

    run_dir: Path
    root: RunRoot
    cfg: BuildConfig


@dataclass(frozen=True)
class RunScan:
    """Result of :func:`enumerate_workspace_runs`: what was found, and what was skipped."""

    candidates: list[RunCandidate]
    skipped: list[SkippedRoot]


def _workspace_roots(workspace: Path) -> list[RunRoot]:
    """The four literal family/build roots this change covers for ``workspace``.

    ``<workspace>/nxp``, ``<workspace>/ti``, ``<workspace>`` itself (plain
    ``build/runs``), and every ``<workspace>/build-*`` (meta-avocado/generic
    fanout). Does not cover preset-fanout archival subdirectories or a
    non-``"build"`` TOPDIR name - out of scope per design.md's Non-Goals.
    This list is this module's own literal (Assumption A5): it is defined
    directly here, never imported from ``bakar.commands.*``.
    """
    roots = [
        RunRoot(bsp_root=workspace / "nxp", family="nxp", resolve_workspace=workspace, resolve_family="nxp"),
        RunRoot(bsp_root=workspace / "ti", family="ti", resolve_workspace=workspace, resolve_family="ti"),
        RunRoot(bsp_root=workspace, family="generic", resolve_workspace=workspace, resolve_family="bbsetup"),
    ]
    try:
        build_dirs = sorted(p for p in workspace.glob("build-*") if p.is_dir())
    except OSError:
        build_dirs = []
    roots.extend(
        RunRoot(bsp_root=p, family="generic", resolve_workspace=p, resolve_family="bbsetup") for p in build_dirs
    )
    return roots


def enumerate_workspace_runs(path: Path, *, user_config: UserConfig | None = None) -> RunScan:
    """Enumerate every candidate run directory across accessible roots.

    ``path`` is either a full workspace (scanned for the four
    :func:`_workspace_roots` family/build roots) or a bare runs directory -
    e.g. ``<topdir>/build/runs``, the shape a host-mode ``/proc`` scan
    discovers without ever resolving a workspace. bakar always names this
    directory literally ``"runs"`` (:attr:`BuildConfig.runs_dir`), so a
    ``path`` whose final component is ``"runs"`` is treated as one
    pre-resolved root instead of being probed for ``nxp``/``ti``/``build``/
    ``build-*`` subdirectories.

    For each candidate root, :func:`bakar.config.resolve` is called directly
    - only the workspace path is a required input; ``workspace_config`` (the
    per-workspace ``.bakar.toml`` tier) is auto-loaded by ``resolve()``
    itself from each root's own ``resolve_workspace``, with no action needed
    here. ``user_config`` (the ``~/.config/bakar/config.toml`` tier) is NOT
    auto-loaded - pass it explicitly via this function's own ``user_config``
    parameter when the caller has one (e.g. from the CLI layer's
    ``_state._USER_CONFIG``), or every root resolves as if that tier were
    empty, silently diverging from a caller that resolves the same
    workspace with it supplied. A root whose family directory does not exist
    on disk contributes nothing and is never resolved. A root whose
    resolution raises (e.g. a malformed ``.bakar.toml``) is excluded from
    the result entirely - the same isolation a malformed launch record gets
    in :func:`live_workspace_runs` below - so one broken root cannot crash
    the whole scan.

    The resolved config gates two things: the NFS lock-ownership check runs
    BEFORE anything under the root is read - a root the gate refuses is
    excluded from ``candidates`` and reported in :attr:`RunScan.skipped` with
    the refusal reason, never silently treated as "nothing running there" -
    and it supplies every run discovered under that root with its
    family/machine metadata, since that is a property of the root, not of
    the individual run.
    """
    if path.name == "runs":
        bsp_root = path.parent.parent
        # Mirror _workspace_roots' own per-root convention: an nxp/ti root is
        # identified by its directory name and resolved against its PARENT
        # workspace with the matching family, exactly as _workspace_roots
        # builds `workspace / "nxp"` and `workspace / "ti"`. Anything else
        # (a bare `build/runs` root, or a `build-*` fanout root) falls back
        # to the generic/bbsetup resolution the previous unconditional
        # assignment always used - a host-mode /proc scan has no workspace
        # in hand, so the directory name is the only signal available here.
        if bsp_root.name in ("nxp", "ti"):
            roots = [
                RunRoot(
                    bsp_root=bsp_root,
                    family=bsp_root.name,
                    resolve_workspace=bsp_root.parent,
                    resolve_family=bsp_root.name,
                )
            ]
        else:
            roots = [RunRoot(bsp_root=bsp_root, family="generic", resolve_workspace=bsp_root, resolve_family="bbsetup")]
    else:
        roots = _workspace_roots(path)

    candidates: list[RunCandidate] = []
    skipped: list[SkippedRoot] = []
    for root in roots:
        if not root.bsp_root.is_dir():
            continue
        try:
            cfg = resolve(
                ResolveRequest(
                    workspace=root.resolve_workspace, bsp_family=root.resolve_family, user_config=user_config
                )
            )
        except Exception as exc:  # noqa: BLE001 - one broken root's config must not crash the scan
            # Not recorded in `skipped`: that list is reserved for the NFS
            # lock-ownership gate's own refusal reasons (SkippedRoot.refusal
            # is typed as LockRefusal), and a config-resolution failure - a
            # malformed .bakar.toml, for instance - is a different failure
            # class. Logged instead so the drop is traceable rather than
            # fully silent.
            _logger.warning("skipping root %s: config resolution failed: %s", root.bsp_root, exc)
            continue
        refusal = build_stop.lock_mutation_guard(cfg)
        if refusal is not None:
            skipped.append(SkippedRoot(root=root, refusal=refusal))
            continue
        try:
            run_dirs = sorted(root.runs_dir.iterdir())
        except OSError:
            continue
        candidates.extend(RunCandidate(run_dir=d, root=root, cfg=cfg) for d in run_dirs if d.is_dir())

    return RunScan(candidates=candidates, skipped=skipped)


def is_candidate_live(candidate: RunCandidate) -> bool:
    """Report whether ``candidate`` is a currently live build.

    Host mode: the launch record parses as ``mode="host"`` and
    :func:`is_build_running` confirms a live, cmdline-verified PGID.
    Container mode: a recorded container label is a NECESSARY but not
    sufficient signal - queries the runtime via :func:`_container_id_status`
    directly, not :func:`_container_id`, since an unanswerable query
    (``_ERROR``) must not be read as "dead"; only a confirmed ``_DEAD``
    excludes the candidate. Anything else (no ``container_label`` and not
    host mode) is not live. Extracted from :func:`live_workspace_runs` so a
    caller in another module can evaluate a single candidate's liveness
    without going through the whole enumerate-and-filter pipeline; see that
    function's docstring for the full rationale behind each branch.
    """
    record = build_stop.read_launch_record(candidate.run_dir)
    if record.mode == "host":
        alive, _pgid, cmdline_ok = build_stop.is_build_running(candidate.run_dir)
        return alive and cmdline_ok
    if record.container_label is not None:
        runtime = record.runtime or stop_container.detect_runtime()
        status, _cid = stop_container._container_id_status(runtime, record.container_label)
        return status != stop_container._DEAD
    return False


def live_workspace_runs(path: Path, *, user_config: UserConfig | None = None) -> list[RunCandidate]:
    """Live-only filter over :func:`enumerate_workspace_runs`'s candidates.

    Host mode: the launch record parses as ``mode="host"`` and
    :func:`is_build_running` confirms a live, cmdline-verified PGID.
    Container mode: a recorded container label is a NECESSARY but not
    sufficient signal - :func:`remove_pid` deletes the launch record on
    every stop path that reaches it, so a label surviving on disk usually
    means either a genuinely live container or one that exited outside
    ``bakar stop``'s own cleanup (a runtime restart, a manual ``docker
    kill``, a host crash). Treating the label alone as "live" without
    querying the runtime made every such stale record permanently
    unreachable: it counted toward "two or more live builds" in the
    no-``--run`` refuse-and-list branch, yet a targeted ``--run``/pick on it
    reported "not currently live" - a workspace could accumulate stale
    container records with no path that ever cleaned them up. Querying the
    runtime here (the same call an explicit target already had to make)
    closes that gap for every caller of this function, not only the
    explicitly-targeted one.

    The query uses :func:`_container_id_status` directly, not
    :func:`_container_id` - the two-state collapse the latter does (a
    matching container found, or not - folding "confirmed dead" and "the
    query itself failed" into the same "no id" result) is wrong here. An
    unanswerable query (missing runtime binary, unreachable daemon,
    timeout) is not proof the container is gone, and treating it as such
    would drop a genuinely live build out of discovery the moment the
    runtime has a hiccup - reporting "no live builds" or "not currently
    live" for a build that is still running. Only a confirmed ``_DEAD``
    excludes a candidate; ``_ERROR`` fails conservative and keeps it,
    matching how the NFS lock-ownership gate elsewhere in this module
    refuses rather than guesses when it cannot confirm an answer.

    A candidate whose launch record is malformed or unreadable is excluded
    rather than raising: :func:`read_launch_record` already degrades
    gracefully for that case (see its own docstring), so it never produces
    an entry this loop would need to special-case.

    ``user_config`` forwards verbatim to :func:`enumerate_workspace_runs` -
    see its own docstring for why this must be passed explicitly rather than
    relying on an auto-load.
    """
    return [
        candidate
        for candidate in enumerate_workspace_runs(path, user_config=user_config).candidates
        if is_candidate_live(candidate)
    ]


def correlate_host_discoveries(
    discovered: dict[Path, frozenset[int]], *, user_config: UserConfig | None = None
) -> list[RunCandidate]:
    """Correlate group 7's ``/proc``-walk discoveries to live run directories.

    ``discovered`` is :func:`proc_tree._discover_host_cookers`'s return value: a topdir
    (the directory containing ``runs/``) mapped to the cooker PID(s) matched
    there. For each topdir, this scans ``topdir / "runs"`` via
    :func:`live_workspace_runs` - the same per-run enumeration/filter group 2
    already exposes for a bare runs-directory path, not a second reader of
    the launch-record format.

    A topdir whose ``runs/`` has no live candidate underneath it - a
    launch-record write race, or a cooker started outside the normal build
    entry point - is silently dropped: :func:`live_workspace_runs` already
    returns ``[]`` for that case, so no candidate is reported and none is
    fabricated.

    ``user_config`` forwards verbatim to :func:`live_workspace_runs` (and
    from there to :func:`enumerate_workspace_runs`) - see its docstring for
    why an omitted value resolves every root as if that config tier were
    empty.
    """
    candidates: list[RunCandidate] = []
    for topdir in discovered:
        candidates.extend(live_workspace_runs(topdir / "runs", user_config=user_config))
    return candidates


def dedup_across_sources(
    host_candidates: list[RunCandidate], container_candidates: list[ContainerCandidate]
) -> tuple[list[RunCandidate], list[ContainerCandidate]]:
    """Drop host-mode rows whose run id is already reported by container-mode discovery.

    Group 9's host-mode correlation and group 10/11's container-mode
    discovery are independent sources scanning the same set of running
    builds, so the same run id can legitimately show up in both: a
    container-mode build still has a launch record on the host, and
    :func:`correlate_host_discoveries` has no way to know the build it found
    is already reported by the container runtime. When a run id appears in
    both, the container-mode row wins - it carries the container id needed
    to stop the build - so the matching host candidate is dropped here
    rather than rendered as a second, less-actionable row for the same run.

    Container candidates are never dropped by this function; only host
    candidates colliding with a container-mode run id are removed. Both
    lists are returned - the shrunk host list and the unchanged container
    list - because group 15's ``bakar ps`` needs to render rows from both
    sources side by side.
    """
    container_run_ids = {candidate.run_id for candidate in container_candidates}
    deduped_host = [c for c in host_candidates if c.run_dir.name not in container_run_ids]
    return deduped_host, container_candidates
