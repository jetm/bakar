"""bakar ps subcommand - host-wide listing of the live bakar builds.

Unlike every other bakar command, ``ps`` performs no workspace resolution of
any kind: it never looks up a workspace or resolves a
:class:`~bakar.config.BuildConfig` from the current directory, so it runs
correctly from anywhere on the host, including outside any bakar workspace.

Aggregates group 13's deduplicated host-wide discovery from
:mod:`bakar.run_discovery` and :mod:`bakar.stop_container`:

- host-mode builds, found by a ``/proc`` walk for bitbake cooker argv markers
  (``_discover_host_cookers``) and correlated to their live run directories
  (``correlate_host_discoveries``);
- container-mode builds, found by querying the container runtime for every
  running container carrying a ``bakar.run_id`` label
  (``discover_running_containers_or_warn``), collapsed to one row per run id
  (``dedup_container_candidates``);
- the two sources reconciled so a run id reported by both is rendered only as
  its (more actionable) container row (``dedup_across_sources``);
- detached or dispatched builds whose unit is active but has not started
  bitbake yet (``detach_records.active_units``), as ``unit`` rows with unknown
  family and machine, skipped when their run id is already a row.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer

import bakar.commands._app as _state
from bakar import build_stop, detach_records, proc_tree, run_discovery, stop_container
from bakar.commands._app import app, console
from bakar.commands._helpers import _run_started_epoch
from bakar.fmt import fmt_duration

_UNKNOWN = "unknown"

# The destination path kas-container bind-mounts KAS_WORK_DIR to inside the
# container - see steps/kas_build.py's ``_container_eventlog_path``, which
# documents this same mapping for the bitbake-eventlog path. This is the
# mount whose host-side ``Source`` recovers the build directory for a
# container-mode row.
_WORK_MOUNT_DESTINATION = "/work"

# Bound on the runtime inspect call, matching build_stop's own runtime-query
# timeout so a wedged daemon cannot hang `bakar ps` indefinitely.
_RUNTIME_QUERY_TIMEOUT_S = 30.0


@dataclass(frozen=True)
class _Row:
    """One rendered ``bakar ps`` row."""

    run_id: str
    mode: str
    family: str
    machine: str
    elapsed_seconds: float


def _elapsed_seconds(run_dir: Path | None, now: float) -> float:
    """Elapsed seconds since ``run_dir`` started, or ``0`` when unknown.

    Wraps :func:`bakar.commands._helpers._run_started_epoch`, which returns
    ``None`` when the run-dir name does not parse (or ``run_dir`` is
    unavailable, e.g. a container row whose mount could not be resolved).
    The conversion of that absent value to the literal ``0`` happens HERE,
    in this command's own row-building code, not inside the shared helper.
    """
    if run_dir is None:
        return 0
    started = _run_started_epoch(run_dir)
    if started is None:
        return 0
    return max(0.0, now - started)


def _container_mount_source(runtime: str, container_id: str) -> str | None:
    """Recover the host-side source path of ``container_id``'s ``/work`` bind mount.

    Runs ``<runtime> inspect -f '{{json .Mounts}}' <container_id>`` - the same
    ``-f`` flag form build_stop's own inspect calls use - and parses the
    JSON array for the entry whose
    ``Destination`` is ``/work`` (see :data:`_WORK_MOUNT_DESTINATION`). Returns
    ``None`` when the inspect call fails or times out, its output does not
    parse as JSON, or no mount matches - never raises, so the caller falls
    back to the "unknown" placeholder rather than failing the row.
    """
    try:
        result = subprocess.run(
            [runtime, "inspect", "-f", "{{json .Mounts}}", container_id],
            capture_output=True,
            text=True,
            check=False,
            timeout=_RUNTIME_QUERY_TIMEOUT_S,
        )
    except OSError, subprocess.TimeoutExpired:
        return None
    if result.returncode != 0:
        return None
    try:
        mounts = json.loads(result.stdout)
    except ValueError:
        return None
    if not isinstance(mounts, list):
        return None
    for mount in mounts:
        if isinstance(mount, dict) and mount.get("Destination") == _WORK_MOUNT_DESTINATION:
            source = mount.get("Source")
            if isinstance(source, str) and source:
                return source
    return None


def _container_row_info(runtime: str, candidate: stop_container.ContainerCandidate) -> tuple[str, str, Path | None]:
    """Resolve ``(family, machine, run_dir)`` for a container-mode candidate.

    Recovers the container's own build-directory bind-mount host-side source
    path (:func:`_container_mount_source`) - this is the container's
    ``KAS_WORK_DIR``, which is the bsp_root for an nxp/ti build but the
    WORKSPACE itself for a meta-avocado build (``steps/kas_build.py``'s
    ``_build_env``: ``KAS_WORK_DIR = cfg.workspace`` when
    ``cfg.is_meta_avocado``, else ``cfg.bsp_root``). Only an nxp/ti bsp_root
    is routed through the bare-runs-path branch (by appending its own
    literal ``build/runs`` suffix, identified by the mount source's own
    directory name) so family resolves BY NAME instead of being misresolved
    as generic/bbsetup - the same fix host-mode discovery's bare-runs-path
    branch already applies. Every other shape (a meta-avocado workspace, a
    plain bbsetup workspace, or any other bsp_root) is passed to
    :func:`bakar.run_discovery.enumerate_workspace_runs` UNCHANGED, so it takes
    the workspace-scan branch, which already includes the passed root itself
    as a generic candidate and globs its ``build-*`` fanout roots - the same
    resolution this call used before the nxp/ti-only special case was added,
    and the only one that finds a meta-avocado build's real
    ``<workspace>/build-<stem>/build/runs``.
    Falls back to ``(_UNKNOWN, _UNKNOWN, None)`` whenever the mount path
    cannot be recovered, or the recovered path does not yield a readable run
    record for this candidate's run id - never raises.
    """
    mount_source = _container_mount_source(runtime, candidate.container_id)
    if mount_source is None:
        return _UNKNOWN, _UNKNOWN, None
    mount_path = Path(mount_source)
    scan_path = mount_path / "build" / "runs" if mount_path.name in ("nxp", "ti") else mount_path
    try:
        scan = run_discovery.enumerate_workspace_runs(scan_path, user_config=_state._USER_CONFIG)
    except Exception:  # noqa: BLE001 - a broken/unreadable mount path must not fail the row
        return _UNKNOWN, _UNKNOWN, None
    for run_candidate in scan.candidates:
        if run_candidate.run_dir.name == candidate.run_id:
            return run_candidate.root.family, run_candidate.cfg.machine, run_candidate.run_dir
    return _UNKNOWN, _UNKNOWN, None


def _collect_rows() -> list[_Row]:
    """Aggregate group 13's deduplicated host-wide discovery into display rows.

    Performs no workspace resolution: every discovery call here is host-wide
    (a ``/proc`` walk, or a container-runtime query with no ``--filter`` on a
    workspace-scoped label), so this runs correctly from any directory.
    """
    now = time.time()

    discovered = proc_tree._discover_host_cookers()
    host_candidates = run_discovery.correlate_host_discoveries(discovered, user_config=_state._USER_CONFIG)

    runtime = stop_container.detect_runtime()
    container_candidates, warning = stop_container.discover_running_containers_or_warn(runtime)
    if warning is not None:
        build_stop._say(warning)
    container_candidates = stop_container.dedup_container_candidates(container_candidates)

    host_candidates, container_candidates = run_discovery.dedup_across_sources(host_candidates, container_candidates)

    rows: list[_Row] = [
        _Row(
            run_id=candidate.run_dir.name,
            mode="host",
            family=candidate.root.family,
            machine=candidate.cfg.machine,
            elapsed_seconds=_elapsed_seconds(candidate.run_dir, now),
        )
        for candidate in host_candidates
    ]
    for candidate in container_candidates:
        family, machine, run_dir = _container_row_info(runtime, candidate)
        rows.append(
            _Row(
                run_id=candidate.run_id,
                mode="container",
                family=family,
                machine=machine,
                elapsed_seconds=_elapsed_seconds(run_dir, now),
            )
        )
    seen = {row.run_id for row in rows}
    for unit in detach_records.active_units():
        latest = next(
            (rec for rec in reversed(detach_records.read_registrations(unit)) if isinstance(rec.get("run_id"), str)),
            None,
        )
        run_id = latest["run_id"] if latest is not None else unit
        if run_id in seen:
            continue
        seen.add(run_id)
        run_dir = latest.get("run_dir") if latest is not None else None
        rows.append(
            _Row(
                run_id=run_id,
                mode="unit",
                family=_UNKNOWN,
                machine=_UNKNOWN,
                elapsed_seconds=_elapsed_seconds(Path(run_dir) if isinstance(run_dir, str) else None, now),
            )
        )
    return rows


@app.command("ps")
def ps(
    json_out: Annotated[
        bool,
        typer.Option("--json", help="Emit a JSON array to stdout instead of plain text."),
    ] = False,
    on: Annotated[
        str | None,
        typer.Option("--on", help="List the builds on this host over ssh instead of locally."),
    ] = None,
) -> None:
    """List the live bakar builds on this host.

    Directory-independent: performs no workspace resolution and runs
    correctly from anywhere, including outside any bakar workspace. Combines
    host-mode builds (``host``, discovered by scanning ``/proc`` for bitbake
    cookers), container-mode builds (``container``, discovered by querying the
    container runtime) and ``unit`` rows: a detached or dispatched build whose
    unit is active but has not started bitbake yet, with unknown family and
    machine. One row per live build - run id, mode, family, and machine. With
    ``--json`` each row is emitted as an object with exactly five fields
    (``run_id``, ``mode``, ``family``, ``machine``, ``elapsed_seconds`` as a
    JSON integer); an empty result emits ``[]``. ``--on HOST`` runs ``bakar
    ps`` on that host over ssh and mirrors its output and exit status.
    """
    if on is not None:
        from bakar.commands.status import REMOTE_STATUS_TIMEOUT, run_on_host

        raise typer.Exit(
            code=run_on_host(
                on,
                "ps",
                ["--json"] if json_out else [],
                None,
                capture_timeout=REMOTE_STATUS_TIMEOUT,
                transport_exit=2,
            )
        )
    rows = _collect_rows()
    if json_out:
        print(
            json.dumps(
                [
                    {
                        "run_id": row.run_id,
                        "mode": row.mode,
                        "family": row.family,
                        "machine": row.machine,
                        "elapsed_seconds": int(row.elapsed_seconds),
                    }
                    for row in rows
                ]
            )
        )
        return
    if not rows:
        console.print("no bakar builds running")
        return
    for row in rows:
        console.print(
            f"{row.run_id}  mode={row.mode}  family={row.family}  "
            f"machine={row.machine}  elapsed={fmt_duration(row.elapsed_seconds)}"
        )
