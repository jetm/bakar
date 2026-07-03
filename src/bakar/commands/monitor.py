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
from bakar import hashserv, prserv
from bakar.build_stop import is_build_running
from bakar.cache_render import cluster_doc, daemon_doc, render_cluster, render_sccache_cache
from bakar.commands._app import app, console
from bakar.commands._helpers import WorkspaceOption, _bsp_from_cwd, _dispatch_from_yaml, _resolve_workspace
from bakar.commands.log import _resolve_run_dir
from bakar.config import BSPSpec, BuildConfig, resolve
from bakar.diagnostics import probe_build_daemon, probe_cluster
from bakar.eventlog import normalize, running_tasks
from bakar.steps.build_ui import SEVERITY_PASSTHROUGH, _fmt_stall, _task_style

if TYPE_CHECKING:
    from bakar.diagnostics import BuildDaemonReport

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

    succeeded = 0
    failed = 0
    setscene_rerun = 0
    for row in tasks:
        outcome = row.get("outcome")
        if outcome == "succeeded":
            succeeded += 1
        elif outcome == "failed":
            failed += 1
        elif outcome == "failed_silent":
            # setscene (sstate-restore) failure: bitbake re-runs the real task,
            # so this is a recovered cache miss, NOT a build failure. Count it
            # separately so the monitor never reports it as "N failed".
            setscene_rerun += 1

    # Running-task selection is owned by eventlog.running_tasks so this view and
    # bakar stop cannot drift on which rows count as running.
    running = running_tasks(run_dir)

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
        "tasks_setscene_rerun": setscene_rerun,
        "running": [{"recipe": r.recipe, "task": r.task} for r in running],
        "failures": artifact["failures"][-_FAILURE_TAIL:],
    }


def _split_host_port(endpoint: str, default_port: int) -> tuple[str, int]:
    """Split a ``host:port`` endpoint; fall back to ``default_port`` if no numeric port."""
    host, sep, port = endpoint.rpartition(":")
    if sep and port.isdigit():
        return host, int(port)
    return endpoint, default_port


def _central_daemon_status(cfg: BuildConfig) -> dict[str, Any]:
    """Report the central cross-node tier endpoints + liveness, or {} when unconfigured.

    When ``cfg.bb_hashserve`` / ``cfg.prserv_host`` are set the build points
    BB_HASHSERVE / PRSERV_HOST at the shared Rust/PostgreSQL services, so probe
    those endpoints (a plain TCP connect) rather than the per-workspace ports.
    """
    status: dict[str, Any] = {}
    if cfg.bb_hashserve:
        host, port = _split_host_port(cfg.bb_hashserve, hashserv.CENTRAL_DEFAULT_PORT)
        status["hashserv"] = {"url": cfg.bb_hashserve, "running": hashserv.central_listening(host, port)}
    if cfg.prserv_host:
        host, port = _split_host_port(cfg.prserv_host, prserv.CENTRAL_DEFAULT_PORT)
        status["prserv"] = {"host": cfg.prserv_host, "running": prserv.central_listening(host, port)}
    return status


def _daemon_status(cfg: BuildConfig) -> dict[str, Any]:
    """Resolve the cache-daemon addresses for the run, toggling on config.

    Two tiers, selected by config:

    - **Central** (``cfg.bb_hashserve`` / ``cfg.prserv_host`` set): the build
      points BB_HASHSERVE / PRSERV_HOST at the shared Rust/PostgreSQL services
      and never starts the per-workspace bitbake daemons, so report and probe
      the central endpoints. Takes precedence whenever configured.
    - **Per-workspace** (host mode, no central tier): bakar manages a
      bitbake-hashserv/prserv daemon keyed to the shared sstate dir and bound to
      ``cfg.cluster_bind_host``; report those addresses plus a liveness probe.

    Returns an empty dict when neither applies (a container build with no
    central tier), so the caller suppresses the daemon line.
    """
    central = _central_daemon_status(cfg)
    if central:
        return central
    if not cfg.host_mode:
        return {}
    bind_host = cfg.cluster_bind_host or "localhost"
    hs_port = hashserv._workspace_port(cfg.hashserv_state_key)
    pr_port = prserv._workspace_port(cfg.prserv_state_key)
    return {
        "hashserv": {
            "url": f"ws://{bind_host}:{hs_port}",
            "running": hashserv.is_running(cfg.hashserv_state_key),
        },
        "prserv": {
            "host": f"{bind_host}:{pr_port}",
            "running": prserv.is_running(cfg.prserv_state_key, bind_host=bind_host),
        },
    }


def _render_daemons(daemons: dict[str, Any]) -> Text | None:
    """Render the managed cluster-cache daemon addresses as one line.

    Returns ``None`` when no daemons are managed (non-host build) so the caller
    suppresses the line entirely rather than printing an empty header.
    """
    if not daemons:
        return None
    rendered: list[tuple[str, str, bool]] = []
    hs = daemons.get("hashserv")
    if hs:
        rendered.append(("hashserv", hs["url"].removeprefix("ws://"), hs["running"]))
    pr = daemons.get("prserv")
    if pr:
        rendered.append(("prserv", pr["host"], pr["running"]))
    if not rendered:
        return None

    line = Text("daemons: ", style="bold")
    for i, (name, addr, running) in enumerate(rendered):
        if i:
            line.append(", ")
        line.append(f"{name} {addr} ")
        line.append("(up)" if running else "(down)", style="green" if running else "red")
    return line


def _snapshot(run_dir: Path, url: str | None, daemon_probe: _DaemonProbe, daemons: dict[str, Any]) -> dict[str, Any]:
    """Assemble one monitor snapshot doc (cluster + build daemon + build progress)."""
    report = probe_cluster(url)
    daemon = daemon_probe.get()
    return {
        "run": run_dir.name,
        "cluster": cluster_doc(report, url),
        "build_daemon": daemon_doc(daemon),
        "build": _build_progress(run_dir),
        "daemons": daemons,
    }


def _render(snapshot: dict[str, Any]) -> Group:
    """Render a monitor snapshot dict as a light Rich renderable for Live."""
    parts: list[Any] = []

    parts.extend(render_cluster(snapshot["cluster"]))
    parts.append(render_sccache_cache(snapshot["build_daemon"]))

    daemon_line = _render_daemons(snapshot.get("daemons") or {})
    if daemon_line is not None:
        parts.append(daemon_line)

    build = snapshot["build"]
    state = "live" if build["live"] else (build["outcome"] or "unknown")
    elapsed = build["elapsed_seconds"]
    elapsed_txt = _fmt_stall(int(elapsed)) if elapsed is not None else "?"
    done = build["tasks_done"] or 0
    total = build["tasks_total"]
    remaining = build["tasks_remaining"]
    if total:
        pct = f" {100 * done // total}%"
        tasks_txt = f"{done}/{total} tasks ({remaining} left){pct}"
    else:
        tasks_txt = f"{done} tasks done (total pending)"
    progress = Text("build: ", style="bold")
    progress.append(f"[{state}] ")
    progress.append(f"{tasks_txt}, {build['tasks_running']} running, ")
    failed_n = build["tasks_failed"]
    progress.append(f"{failed_n} failed", style="bold red" if failed_n else None)
    rerun_n = build.get("tasks_setscene_rerun") or 0
    if rerun_n:
        # recovered sstate-restore rejections, not build failures
        progress.append(f", {rerun_n} setscene re-runs", style="dim")
    progress.append(f"  elapsed {elapsed_txt}")
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
    workspace: WorkspaceOption = None,
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
    daemons = _daemon_status(cfg)

    if output_json and not watch:
        doc = _snapshot(run_dir, url, daemon_probe, daemons)
        typer.echo(json.dumps(doc, indent=2))
        return

    if output_json and watch:
        _run_watch(run_dir, url, daemon_probe, daemons, interval)
        return

    if once:
        snapshot = _snapshot(run_dir, url, daemon_probe, daemons)
        snapshot["kas_errors"] = _recent_kas_errors(run_dir / "kas.log")
        console.print(_render(snapshot))
        return

    _run_live(run_dir, url, daemon_probe, daemons, interval)


def _run_watch(
    run_dir: Path, url: str | None, daemon_probe: _DaemonProbe, daemons: dict[str, Any], interval: float
) -> None:
    """Stream NDJSON snapshots to stdout until the build finishes (one final snapshot)."""
    try:
        while True:
            doc = _snapshot(run_dir, url, daemon_probe, daemons)
            typer.echo(json.dumps(doc))
            if not doc["build"]["live"]:
                return
            time.sleep(interval)
    except KeyboardInterrupt:
        raise typer.Exit(code=0) from None


def _run_live(
    run_dir: Path, url: str | None, daemon_probe: _DaemonProbe, daemons: dict[str, Any], interval: float
) -> None:
    """Refresh a Rich live view on stderr until the build finishes, then a final frame."""
    from rich.live import Live

    def _snapshot_with_errors() -> dict[str, Any]:
        snapshot = _snapshot(run_dir, url, daemon_probe, daemons)
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
