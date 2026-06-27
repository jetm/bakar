"""bakar monitor subcommand - one-view watch for a long sccache-dist build.

Aggregates three live signals for an in-flight Yocto/OE build:

- sccache-dist cluster load (``probe_cluster``)
- the in-container build daemon's cache/dist stats (``probe_build_daemon``)
- bitbake task progress and recent failures, read from the run's event log

Two output modes mirror the rest of the CLI. The default is a refreshing
Rich live view on stderr; ``--json`` emits a single snapshot to stdout (and
exits), and ``--json --watch`` streams one compact NDJSON object per refresh
interval to stdout. Human/Rich output always goes to stderr so a piped
``--json`` consumer never sees decoration.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer
from rich.console import Group
from rich.table import Table
from rich.text import Text

import bakar.commands._app as _state
from bakar.build_stop import is_build_running
from bakar.commands._app import app, console
from bakar.commands._helpers import _bsp_from_cwd, _dispatch_from_yaml, _resolve_workspace
from bakar.commands.log import _resolve_run_dir
from bakar.config import BSPSpec, resolve
from bakar.diagnostics import probe_build_daemon, probe_cluster
from bakar.eventlog import normalize
from bakar.steps.build_ui import SEVERITY_PASSTHROUGH, _fmt_stall, _task_style

if TYPE_CHECKING:
    from bakar.diagnostics import BuildDaemonReport, ClusterReport

# probe_build_daemon shells out to docker twice with 15s timeouts; never call
# it more than once per this window so a fast refresh interval cannot stack
# heavy docker execs.
_DAEMON_THROTTLE_SECONDS = 3.0

# Tail length for the recent-failures panel.
_FAILURE_TAIL = 5

# How many kas.log severity lines to scan/keep.
_KAS_TAIL_LINES = 400


class _DaemonProbe:
    """Throttle wrapper around :func:`probe_build_daemon`.

    The probe runs two ``docker exec`` calls with 15s timeouts, so calling it
    on every fast refresh would stack heavy subprocesses. This caches the last
    report and only re-probes once ``_DAEMON_THROTTLE_SECONDS`` have elapsed
    (monotonic clock), returning the cached value in between.
    """

    def __init__(self, throttle: float = _DAEMON_THROTTLE_SECONDS) -> None:
        self._throttle = throttle
        self._cached: BuildDaemonReport | None = None
        self._last: float | None = None

    def get(self) -> BuildDaemonReport:
        now = time.monotonic()
        if self._cached is None or self._last is None or (now - self._last) >= self._throttle:
            self._cached = probe_build_daemon()
            self._last = now
        return self._cached


def _resolve_scheduler_url(scheduler: str | None) -> str | None:
    """Resolve the scheduler URL the same way ``cluster-info`` does."""
    url = scheduler
    if url is None:
        url = _state._SCCACHE_SCHEDULER
    if url is None and _state._USER_CONFIG is not None:
        url = _state._USER_CONFIG.sccache_scheduler_url
    return url


def _recent_kas_errors(kas_log: Path, limit: int = _FAILURE_TAIL) -> list[str]:
    """Return the last ``limit`` ERROR/FATAL lines from ``kas_log``.

    Reuses ``build_ui.SEVERITY_PASSTHROUGH`` for the match but keeps only the
    hard-failure severities (ERROR/FATAL); WARNING/QA Issue lines are noise for
    a glance-and-go failure tail. Best-effort: a missing or unreadable log
    yields an empty list rather than raising.
    """
    try:
        text = kas_log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    hits: list[str] = []
    for line in text.splitlines()[-_KAS_TAIL_LINES:]:
        m = SEVERITY_PASSTHROUGH.search(line)
        if m and m.group(1) in ("ERROR", "FATAL"):
            hits.append(line.strip())
    return hits[-limit:]


def _run_started_epoch(run_dir: Path) -> float | None:
    """Best-effort build start time (epoch seconds) from the run-dir name.

    bitbake's BuildStarted event carries no timestamp, so the event log cannot
    supply one. The run directory is named ``YYYYMMDD-HHMMSS`` at the local
    wall-clock start, so parse that. Returns None when the name does not parse.
    """
    try:
        return time.mktime(time.strptime(run_dir.name, "%Y%m%d-%H%M%S"))
    except ValueError, OverflowError:
        return None


def _build_progress(run_dir: Path) -> dict[str, Any]:
    """Summarize bitbake task progress for ``run_dir`` from its event log.

    Reports the runqueue's planned/done/remaining task counts (so the view
    shows how far the build has to go), the currently-running tasks, elapsed
    wall time, and a tail of failures. Task totals come from the latest
    ``runQueueTaskStarted.stats`` the event log carries; elapsed is derived
    from the run-dir name because BuildStarted has no timestamp. Reads only
    through :mod:`bakar.eventlog`, which decodes the base64-pickled raw log
    without importing bitbake.
    """
    artifact = normalize(run_dir / "bitbake_eventlog.json")
    tasks = artifact["tasks"]

    running: list[dict[str, Any]] = []
    succeeded = 0
    failed = 0
    for row in tasks:
        outcome = row.get("outcome")
        if outcome == "succeeded":
            succeeded += 1
        elif outcome in ("failed", "failed_silent"):
            failed += 1
        elif outcome is None and row.get("started") is not None:
            running.append(row)

    build = artifact["build"]
    completed = build.get("completed")

    start_epoch = _run_started_epoch(run_dir)
    if start_epoch is not None:
        try:
            end = float(completed) if completed is not None else time.time()
        except TypeError, ValueError:
            end = time.time()
        elapsed = max(0.0, end - start_epoch)
    else:
        elapsed = None

    # Runqueue total/completed (matches bitbake's "X of Y", which counts
    # setscene-covered tasks the executed-task log does not). Fall back to the
    # executed-task success count until the runqueue total is known.
    planned = build.get("tasks_total")
    rq_done = build.get("tasks_completed")
    done = rq_done if rq_done is not None else succeeded
    remaining = (planned - done) if (planned is not None and done is not None) else None

    live, _pgid, _cmdline_ok = is_build_running(run_dir)

    return {
        "outcome": build.get("outcome"),
        "live": live,
        "started": build.get("started"),
        "completed": completed,
        "elapsed_seconds": elapsed,
        "tasks_total": planned,
        "tasks_done": done,
        "tasks_remaining": remaining,
        "tasks_running": len(running),
        "tasks_failed": failed,
        "running": [{"recipe": r.get("recipe"), "task": r.get("task")} for r in running],
        "failures": artifact["failures"][-_FAILURE_TAIL:],
    }


def _cluster_doc(report: ClusterReport, url: str | None) -> dict[str, Any]:
    cap = report.capacity
    return {
        "reachable": report.reachable,
        "scheduler_url": url,
        "error": report.error,
        "capacity": (
            {
                "num_servers": cap.num_servers,
                "num_cpus": cap.num_cpus,
                "in_progress": cap.in_progress,
                "servers": cap.servers,
            }
            if cap is not None
            else None
        ),
    }


def _daemon_doc(daemon: BuildDaemonReport) -> dict[str, Any] | None:
    if not daemon.running:
        return None
    return {
        "container": daemon.container,
        "error": daemon.error,
        "cache_hits": daemon.cache_hits,
        "cache_misses": daemon.cache_misses,
        "distributed": daemon.distributed,
        "dist_errors": daemon.dist_errors,
        "cache_location": daemon.cache_location,
        "per_node": dict(daemon.per_node),
        "verdict": daemon.verdict,
    }


def _snapshot(run_dir: Path, url: str | None, daemon_probe: _DaemonProbe) -> dict[str, Any]:
    """Assemble one monitor snapshot doc (cluster + build daemon + build progress)."""
    report = probe_cluster(url)
    daemon = daemon_probe.get()
    return {
        "run": run_dir.name,
        "cluster": _cluster_doc(report, url),
        "build_daemon": _daemon_doc(daemon),
        "build": _build_progress(run_dir),
    }


def _render(snapshot: dict[str, Any]) -> Group:
    """Render a monitor snapshot dict as a light Rich renderable for Live."""
    parts: list[Any] = []

    cluster = snapshot["cluster"]
    cap = cluster["capacity"]
    if cluster["reachable"] and cap is not None:
        line = Text("cluster: ", style="bold")
        line.append(f"{cap['num_servers']} server(s), {cap['num_cpus']} cpus, {cap['in_progress']} job(s) in progress")
        parts.append(line)
        servers = cap["servers"]
        if servers:
            for node in servers:
                if isinstance(node, dict):
                    parts.append(
                        Text(
                            f"  {node.get('id', '?')} - {node.get('num_cpus', '?')} cpus, "
                            f"{node.get('in_progress', 0)} job(s)",
                            style="dim",
                        )
                    )
                else:
                    parts.append(Text(f"  {node}", style="dim"))
    else:
        parts.append(Text(f"cluster: unreachable ({cluster['error']})", style="red"))

    daemon = snapshot["build_daemon"]
    if daemon is None:
        parts.append(Text("daemon: no build container running", style="dim"))
    elif daemon["error"]:
        parts.append(Text(f"daemon: stats unavailable ({daemon['error']})", style="yellow"))
    else:
        colour = {"DISTRIBUTING": "green", "LOCAL-ONLY": "red"}.get(daemon["verdict"], "yellow")
        local = max(daemon["cache_misses"] - daemon["distributed"], 0)
        line = Text("daemon: ", style="bold")
        line.append(f"{daemon['verdict']}", style=colour)
        line.append(
            f"  cache {daemon['cache_hits']}/{daemon['cache_misses']} hit/miss  "
            f"dist {daemon['distributed']} (local {local}, errors {daemon['dist_errors']})"
        )
        parts.append(line)

    build = snapshot["build"]
    state = "live" if build["live"] else (build["outcome"] or "unknown")
    elapsed = build["elapsed_seconds"]
    elapsed_txt = _fmt_stall(int(elapsed)) if elapsed is not None else "?"
    done = build["tasks_done"] or 0
    total = build["tasks_total"]
    remaining = build["tasks_remaining"]
    if total:
        pct = f" {100 * done // total}%" if total else ""
        tasks_txt = f"{done}/{total} tasks ({remaining} left){pct}"
    else:
        tasks_txt = f"{done} tasks done (total pending)"
    progress = Text("build: ", style="bold")
    progress.append(f"[{state}] ")
    progress.append(f"{tasks_txt}, {build['tasks_running']} running, ")
    progress.append(f"{build['tasks_failed']} failed  elapsed {elapsed_txt}")
    parts.append(progress)

    if build["running"]:
        table = Table(show_edge=False, box=None, pad_edge=False)
        table.add_column("task", no_wrap=True)
        table.add_column("recipe", no_wrap=True)
        for row in build["running"][:16]:
            task = row["task"] or "?"
            icon, colour = _task_style(task)
            table.add_row(Text(f"{icon} {task}", style=colour), Text(str(row["recipe"] or "?"), style="dim"))
        parts.append(table)

    failures = build["failures"]
    kas_errors = snapshot.get("kas_errors") or []
    if failures or kas_errors:
        parts.append(Text("recent failures:", style="bold red"))
        parts.extend(Text(f"  {f.get('recipe', '?')} {f.get('task', '?')}", style="red") for f in failures)
        parts.extend(Text(f"  {line}", style="red") for line in kas_errors)

    return Group(*parts)


@app.command("monitor")
def monitor(
    kas_yaml: Annotated[
        Path | None,
        typer.Argument(
            exists=False,
            help="Optional kas YAML; runs live next to it under <yaml-parent>/build/runs/.",
        ),
    ] = None,
    run: Annotated[
        str | None,
        typer.Option("--run", help="Run ID (YYYYMMDD-HHMMSS). Latest if omitted."),
    ] = None,
    scheduler: Annotated[
        str | None,
        typer.Option("--scheduler", help="Scheduler URL override (default: from --sccache-scheduler or config)"),
    ] = None,
    interval: Annotated[
        float,
        typer.Option("--interval", "-n", help="Refresh interval in seconds."),
    ] = 2.0,
    once: Annotated[
        bool,
        typer.Option("--once", help="Render a single snapshot then exit (non-watch)."),
    ] = False,
    output_json: Annotated[
        bool,
        typer.Option("--json", help="Emit one JSON snapshot to stdout and exit."),
    ] = False,
    watch: Annotated[
        bool,
        typer.Option("--watch", help="With --json, stream NDJSON (one object per interval)."),
    ] = False,
    workspace: Annotated[
        Path | None,
        typer.Option("--workspace", "-w", help="Workspace root override"),
    ] = None,
) -> None:
    """Aggregate cluster load, build-daemon stats, and bitbake progress for a run.

    Default output is a refreshing Rich view on stderr. ``--json`` emits one
    snapshot doc to stdout and exits; ``--json --watch`` streams NDJSON, one
    compact object per ``--interval`` to stdout. Pass a positional kas YAML for
    BYO builds; runs are resolved next to the YAML. ``--run`` selects a specific
    run (default: latest).
    """
    if watch and not output_json:
        console.print("[red]--watch is only meaningful with --json[/]")
        raise typer.Exit(code=2)

    if kas_yaml is not None:
        family, _bsp = _dispatch_from_yaml(kas_yaml)
    else:
        ws_probe = _resolve_workspace(workspace, kas_yaml=kas_yaml, family=None)
        family = _bsp_from_cwd(ws_probe) or "nxp"

    ws = _resolve_workspace(workspace, kas_yaml=kas_yaml, family=family)
    cfg = resolve(
        workspace=ws,
        bsp_family=family,
        spec=BSPSpec(manifest=None),
        kas_yaml=kas_yaml,
        user_config=_state._USER_CONFIG,
    )
    runs_dir = cfg.runs_dir
    if not runs_dir.is_dir():
        if output_json:
            typer.echo(json.dumps({"error": "no runs yet; start one with `bakar build`"}, indent=2))
        else:
            console.print("[red]no runs yet[/]; start one with `bakar build`")
        raise typer.Exit(code=1)

    run_dir = _resolve_run_dir(runs_dir, run)
    url = _resolve_scheduler_url(scheduler)
    daemon_probe = _DaemonProbe()

    if output_json and not watch:
        doc = _snapshot(run_dir, url, daemon_probe)
        typer.echo(json.dumps(doc, indent=2))
        return

    if output_json and watch:
        _run_watch(run_dir, url, daemon_probe, interval)
        return

    if once:
        snapshot = _snapshot(run_dir, url, daemon_probe)
        snapshot["kas_errors"] = _recent_kas_errors(run_dir / "kas.log")
        console.print(_render(snapshot))
        return

    _run_live(run_dir, url, daemon_probe, interval)


def _run_watch(run_dir: Path, url: str | None, daemon_probe: _DaemonProbe, interval: float) -> None:
    """Stream NDJSON snapshots to stdout until the build finishes (one final snapshot)."""
    try:
        while True:
            doc = _snapshot(run_dir, url, daemon_probe)
            typer.echo(json.dumps(doc))
            if not doc["build"]["live"]:
                return
            time.sleep(interval)
    except KeyboardInterrupt:
        raise typer.Exit(code=0) from None


def _run_live(run_dir: Path, url: str | None, daemon_probe: _DaemonProbe, interval: float) -> None:
    """Refresh a Rich live view on stderr until the build finishes, then a final frame."""
    from rich.live import Live

    def _snapshot_with_errors() -> dict[str, Any]:
        snapshot = _snapshot(run_dir, url, daemon_probe)
        snapshot["kas_errors"] = _recent_kas_errors(run_dir / "kas.log")
        return snapshot

    try:
        with Live(console=console, refresh_per_second=4) as live:
            while True:
                snapshot = _snapshot_with_errors()
                live.update(_render(snapshot))
                if not snapshot["build"]["live"]:
                    return
                time.sleep(interval)
    except KeyboardInterrupt:
        raise typer.Exit(code=0) from None
