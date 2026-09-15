"""bakar stop subcommand - gracefully halt a running build."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Annotated

import typer

import bakar.commands._app as _state
from bakar import build_stop
from bakar.commands._app import app, console
from bakar.commands._helpers import (
    WorkspaceOption,
    _normalize_dispatch,
    _resolve_workspace,
    _run_started_epoch,
    split_kas_yaml_arg,
)
from bakar.config import BSPSpec, ResolveRequest, resolve
from bakar.fmt import fmt_duration
from bakar.steps import remote_dispatch

# Matches config.py's stop_grace_seconds default. A remote stop resolves no
# BuildConfig - the build is on the other host - so the configured value is not
# reachable here and the default has to be restated.
_REMOTE_STOP_GRACE_SECONDS = 30


def _is_tty() -> bool:
    """Return True when stdin is a TTY. Extracted for testability, matching
    ``commands/presets.py``'s ``_is_tty()`` - a bare ``sys.stdin.isatty()``
    call can't be monkeypatched once ``CliRunner`` has swapped ``sys.stdin``
    for its own captured-input stream.
    """
    return sys.stdin.isatty()


def _stop_matched_run(ws: Path, run_id: str, *, force: bool, timeout: float | None) -> bool:
    """Resolve ``run_id`` against every family root and stop it if live.

    Shared by the ``--run`` flag path and the interactive numbered-pick path so
    there is exactly one stop-dispatch code path regardless of how the run id
    was chosen. Exits nonzero via ``typer.Exit`` when the id has no match, or
    matches a run that is not currently live.
    """
    # Exact-match against the UNFILTERED candidate set, not the live-only one -
    # the unfiltered set is what lets "no match anywhere" and "match but not
    # live" be told apart. A live-only lookup would report both cases
    # identically as "no match".
    scan = build_stop.enumerate_workspace_runs(ws, user_config=_state._USER_CONFIG)
    match = next((c for c in scan.candidates if c.run_dir.name == run_id), None)
    if match is None:
        console.print(f"[red]no run matching {run_id!r} found in this workspace[/].")
        raise typer.Exit(code=1)
    live_run_dirs = {c.run_dir for c in build_stop.live_workspace_runs(ws, user_config=_state._USER_CONFIG)}
    if match.run_dir not in live_run_dirs:
        console.print(f"[red]run {run_id} is not currently live[/].")
        raise typer.Exit(code=1)
    # live_workspace_runs' container-mode check only confirms a launch record
    # was recorded, not that the container is still running (matching bakar
    # stop's existing single-root behavior on purpose, to keep the common
    # no-selector path free of a runtime round-trip). An explicitly selected
    # --run/interactive-pick target needs a real answer, not that cheap
    # compatibility check: query the runtime directly so a finished
    # container build reports "not currently live" here instead of reaching
    # stop_run, whose idempotent stale-cleanup path would otherwise return
    # True and exit 0 for a build that already ended.
    record = build_stop.read_launch_record(match.run_dir)
    if record.mode != "host" and record.container_label is not None:
        runtime = record.runtime or build_stop.detect_runtime()
        if build_stop._container_id(runtime, record.container_label) is None:
            console.print(f"[red]run {run_id} is not currently live[/].")
            raise typer.Exit(code=1)
    grace_seconds = timeout if timeout is not None else match.cfg.stop_grace_seconds
    return build_stop.stop_run(match.run_dir, match.cfg, force=force, grace_seconds=grace_seconds)


@app.command("stop")
def stop(
    kas_yaml: Annotated[
        Path | None,
        typer.Argument(
            exists=False,
            help=(
                "Optional kas YAML, including the colon-joined overlay form "
                "accepted by `bakar build`. Pass the same spec that started the build."
            ),
        ),
    ] = None,
    workspace: WorkspaceOption = None,
    manifest: Annotated[
        str | None,
        typer.Option("--manifest", "-f", help="Manifest filename used to resolve the BSP family"),
    ] = None,
    force: Annotated[
        bool,
        typer.Option("--force", help="Skip the SIGINT grace period and escalate straight to SIGTERM"),
    ] = False,
    timeout: Annotated[
        float | None,
        typer.Option(
            "--timeout",
            help=(
                "Auto-escalate to SIGTERM->SIGKILL after this many seconds of graceful "
                "waiting instead of waiting unbounded for a Ctrl-C. Overrides "
                "[build] stop_grace_seconds; 0 forces the unbounded wait."
            ),
        ),
    ] = None,
    on: Annotated[
        str | None,
        typer.Option(
            "--on",
            help=(
                "Stop the detached build dispatched to this host with `bakar build --on <host>`. "
                "Refuses and lists them when more than one is running; add --all to stop every one."
            ),
        ),
    ] = None,
    all_units: Annotated[
        bool,
        typer.Option(
            "--all",
            help=(
                "With --on, stop every detached build on the host instead of refusing "
                "when more than one is running. On a shared builder the others are "
                "someone else's build."
            ),
        ),
    ] = False,
    run_id: Annotated[
        str | None,
        typer.Option(
            "--run",
            help=(
                "Stop the run whose run directory name exactly matches this id, "
                "resolved against every family root in the workspace rather than "
                "only the one root/kas YAML would resolve. Useful when more than "
                "one build is live in the same workspace at once."
            ),
        ),
    ] = None,
) -> None:
    """Gracefully stop the running build for this workspace's BSP.

    Pass a positional kas YAML for BYO builds (``bakar stop my.yml``), or the
    same colon-joined spec that started the build
    (``bakar stop machine.yml:feature.yml``).

    Runs live under ``<bsp_root>/<build_dir_name>/runs/``, and ``bsp_root``
    depends on the family the head YAML resolves to - for a meta-avocado build
    that is ``workspace/build-<yaml-stem>``, NOT the YAML's own parent, since
    those YAMLs live inside the ``meta-avocado/`` source tree. Passing the
    generated ``build-<machine>/avocado-bakar.yml`` instead of the source YAML
    resolves a different family and therefore a different (empty) runs dir.
    """
    # A remote build runs under a transient unit on the other host and outlives
    # the terminal that dispatched it, so nothing local identifies it: no run
    # dir, no PID file, no workspace to resolve. Short-circuit before any of that
    # is looked up - this is routinely typed from wherever the user happens to
    # be, having lost the dispatching terminal.
    if on is not None:
        grace = timeout if timeout is not None else _REMOTE_STOP_GRACE_SECONDS
        if not remote_dispatch.stop_remote_dispatch(on, force=force, grace_seconds=grace, stop_all=all_units):
            raise typer.Exit(code=1)
        return

    # Split the colon-joined overlay form before dispatching, exactly as `build`
    # does. Stopping a build is done by re-typing the spec that started it, and
    # that spec is routinely `machine.yml:feature-a.yml:feature-b.yml`. Unsplit,
    # the whole string reaches _dispatch_from_yaml as one Path, fails is_file()
    # and exits 2 with "kas YAML not found" - so a build launched with overlays
    # had no supported way to be stopped. Extras are discarded: only the head
    # YAML determines the family and therefore where the run dir lives.
    kas_yaml, _extra_overlays = split_kas_yaml_arg(kas_yaml)
    family, _bsp, kas_yaml, manifest = _normalize_dispatch(kas_yaml, manifest)
    ws = _resolve_workspace(workspace, kas_yaml=kas_yaml, family=family)

    if run_id is not None:
        stopped = _stop_matched_run(ws, run_id, force=force, timeout=timeout)
        if not stopped:
            raise typer.Exit(code=1)
        return

    # No --run: discover what is actually live across every family root before
    # falling back to the single-root path below. Exactly one live build is
    # stopped directly - no listing, no prompt, matching what an operator
    # expects from a bare `bakar stop`. Two or more is ambiguous (which one?):
    # an interactive terminal gets a numbered pick, everything else gets
    # today's refuse-and-list, mirroring `stop_remote_dispatch`'s multi-unit
    # refusal shape. Zero live builds falls through unchanged to the existing
    # single-root `stop_build` call below, including its own stale-lock-cleanup
    # path and messaging.
    #
    # A root the NFS lock-ownership gate refuses is excluded from `live`
    # entirely, so surface it explicitly here - otherwise a peer-held root
    # hiding a real build is indistinguishable from a root with genuinely
    # nothing running, in every branch below (zero, one, or two-or-more live).
    skipped = build_stop.enumerate_workspace_runs(ws, user_config=_state._USER_CONFIG).skipped
    for skipped_root in skipped:
        refusal = skipped_root.refusal
        if refusal.reason == "peer-held":
            host = refusal.host if refusal.host else "another host"
            console.print(
                f"[yellow]{skipped_root.root.bsp_root} is owned by {host}; "
                "run `bakar stop` there to check for a live build[/]"
            )
        else:
            console.print(
                f"[yellow]cannot confirm ownership of {skipped_root.root.bsp_root} "
                f"({refusal.reason}); it was not checked for a live build[/]"
            )
    live = build_stop.live_workspace_runs(ws, user_config=_state._USER_CONFIG)
    if len(live) == 1:
        only = live[0]
        grace_seconds = timeout if timeout is not None else only.cfg.stop_grace_seconds
        stopped = build_stop.stop_run(only.run_dir, only.cfg, force=force, grace_seconds=grace_seconds)
        if not stopped:
            raise typer.Exit(code=1)
        return
    if len(live) >= 2:
        now = time.time()
        rows: list[tuple[build_stop.RunCandidate, str]] = []
        for candidate in live:
            start = _run_started_epoch(candidate.run_dir)
            elapsed = fmt_duration(max(0.0, now - start)) if start is not None else "unknown"
            rows.append((candidate, elapsed))
        console.print(f"[yellow]{len(live)} live builds are running in this workspace[/]:")
        if _is_tty():
            for i, (candidate, elapsed) in enumerate(rows, start=1):
                console.print(
                    f"  [{i}] {candidate.run_dir.name}  family={candidate.root.family}  "
                    f"machine={candidate.cfg.machine}  elapsed={elapsed}"
                )
            choice = typer.prompt("Stop which build", type=int)
            if choice < 1 or choice > len(live):
                console.print(f"[red]{choice} is not a valid choice[/].")
                raise typer.Exit(code=1)
            # Translate the chosen index to a run id and dispatch through the
            # exact same code path the non-interactive `--run` flag uses -
            # there is no separate stopping logic for the interactive case.
            chosen_run_id = live[choice - 1].run_dir.name
            stopped = _stop_matched_run(ws, chosen_run_id, force=force, timeout=timeout)
            if not stopped:
                raise typer.Exit(code=1)
            return
        for candidate, elapsed in rows:
            console.print(
                f"  {candidate.run_dir.name}  family={candidate.root.family}  "
                f"machine={candidate.cfg.machine}  elapsed={elapsed}"
            )
        console.print("refusing to stop more than one - pick one:  bakar stop --run <id>")
        raise typer.Exit(code=1)

    cfg = resolve(
        ResolveRequest(
            workspace=ws,
            bsp_family=family,
            spec=BSPSpec(manifest=manifest),
            kas_yaml=kas_yaml,
            user_config=_state._USER_CONFIG,
        )
    )
    grace_seconds = timeout if timeout is not None else cfg.stop_grace_seconds
    stopped = build_stop.stop_build(cfg.bsp_root, cfg, force=force, grace_seconds=grace_seconds)
    if not stopped:
        raise typer.Exit(code=1)
