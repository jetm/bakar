"""bakar ps subcommand - host-wide listing of every live bakar build.

Unlike every other bakar command, ``ps`` performs no workspace resolution of
any kind: it never calls ``_workspace_from_cwd`` or resolves a
:class:`~bakar.config.BuildConfig` from the current directory, so it runs
correctly from anywhere on the host, including outside any bakar workspace.

Aggregates group 13's deduplicated host-wide discovery from
:mod:`bakar.build_stop`:

- host-mode builds, found by a ``/proc`` walk for bitbake cooker argv markers
  (``_discover_host_cookers``) and correlated to their live run directories
  (``correlate_host_discoveries``);
- container-mode builds, found by querying the container runtime for every
  running container carrying a ``bakar.run_id`` label
  (``discover_running_containers_or_warn``), collapsed to one row per run id
  (``dedup_container_candidates``);
- the two sources reconciled so a run id reported by both is rendered only as
  its (more actionable) container row (``dedup_across_sources``).
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer

from bakar import build_stop
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
    ``-f`` flag form build_stop's own inspect calls use (e.g.
    ``_container_running``) - and parses the JSON array for the entry whose
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


def _container_row_info(runtime: str, candidate: build_stop.ContainerCandidate) -> tuple[str, str, Path | None]:
    """Resolve ``(family, machine, run_dir)`` for a container-mode candidate.

    Recovers the container's own build-directory bind-mount host-side source
    path (:func:`_container_mount_source`) - this is the container's
    ``KAS_WORK_DIR``, i.e. its ``bsp_root``, not a workspace above it. Reads
    that root's run record through :func:`bakar.build_stop.enumerate_workspace_runs`
    by appending its own literal ``build/runs`` suffix first, so the call
    lands on the bare-runs-path branch that resolves a root BY NAME (nxp/ti
    vs generic/bbsetup) instead of the workspace-scan branch, which would
    treat the bsp_root itself as a workspace to search nxp/ti/build-*
    subdirectories under and misresolve every nxp/ti container build as
    generic/bbsetup - the same family-resolution bug host-mode discovery had
    before group 9's bare-runs-path branch learned to check the root's name.
    Falls back to ``(_UNKNOWN, _UNKNOWN, None)`` whenever the mount path
    cannot be recovered, or the recovered path does not yield a readable run
    record for this candidate's run id - never raises.
    """
    mount_source = _container_mount_source(runtime, candidate.container_id)
    if mount_source is None:
        return _UNKNOWN, _UNKNOWN, None
    try:
        scan = build_stop.enumerate_workspace_runs(Path(mount_source) / "build" / "runs")
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

    discovered = build_stop._discover_host_cookers()
    host_candidates = build_stop.correlate_host_discoveries(discovered)

    runtime = build_stop.detect_runtime()
    container_candidates, warning = build_stop.discover_running_containers_or_warn(runtime)
    if warning is not None:
        build_stop._say(warning)
    container_candidates = build_stop.dedup_container_candidates(container_candidates)

    host_candidates, container_candidates = build_stop.dedup_across_sources(host_candidates, container_candidates)

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
    return rows


@app.command("ps")
def ps(
    json_out: Annotated[
        bool,
        typer.Option("--json", help="Emit a JSON array to stdout instead of plain text."),
    ] = False,
) -> None:
    """List every live bakar build on this host.

    Directory-independent: performs no workspace resolution and runs
    correctly from anywhere, including outside any bakar workspace. Combines
    host-mode builds (discovered by scanning ``/proc`` for bitbake cookers)
    and container-mode builds (discovered by querying the container runtime),
    one row per live build - run id, mode, family, and machine. With
    ``--json`` each row is emitted as an object with exactly five fields
    (``run_id``, ``mode``, ``family``, ``machine``, ``elapsed_seconds`` as a
    JSON integer); an empty result emits ``[]``.
    """
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
