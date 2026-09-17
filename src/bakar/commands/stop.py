"""bakar stop subcommand - gracefully halt a running build."""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import typer

import bakar.commands._app as _state
from bakar import build_stop
from bakar.commands._app import app, console
from bakar.commands._helpers import (
    WorkspaceOption,
    _find_workspace_from_cwd,
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


def _confirm_host_wide(candidate: build_stop.RunCandidate) -> bool:
    """Ask before stopping a run outside the caller's own workspace.

    Prints the same identifying row the multi-build listing uses (run id,
    family, machine, elapsed time), then prompts with ``typer.confirm``. On a
    non-interactive terminal (no TTY), the row is still printed but
    ``typer.confirm`` is never called - a non-interactive caller has no way to
    answer, so it must refuse rather than block on input. Reuses the module's
    ``_is_tty`` rather than ``sys.stdin.isatty()`` directly, matching
    ``_is_tty``'s own docstring: a direct stdlib call cannot be monkeypatched
    once the CLI test runner has swapped stdin.
    """
    start = _run_started_epoch(candidate.run_dir)
    elapsed = fmt_duration(max(0.0, time.time() - start)) if start is not None else "unknown"
    console.print(
        f"  {candidate.run_dir.name}  family={candidate.root.family}  "
        f"machine={candidate.cfg.machine}  elapsed={elapsed}"
    )
    if not _is_tty():
        return False
    return typer.confirm(f"Stop {candidate.run_dir.name}?", default=False)


def _stop_matched_run(
    run_id: str,
    *,
    candidates: list[build_stop.RunCandidate],
    live_run_dirs: set[Path],
    skipped: list[build_stop.SkippedRoot],
    force: bool,
    timeout: float | None,
    scope_desc: str = "in this workspace",
    confirm: Callable[[build_stop.RunCandidate], bool] | None = None,
) -> bool:
    """Resolve ``run_id`` against an already-scanned candidate set and stop it if live.

    Shared by the ``--run`` flag path and the interactive numbered-pick path so
    there is exactly one stop-dispatch code path regardless of how the run id
    was chosen. The scan itself is done by the caller, not here - both callers
    already have the ``RunScan``/``live`` data in hand, and scanning twice per
    invocation is wasted work. Exits nonzero via ``typer.Exit`` when the id has
    no match, or matches a run that is not currently live.
    """
    # Exact-match against the UNFILTERED candidate set, not the live-only one -
    # the unfiltered set is what lets "no match anywhere" and "match but not
    # live" be told apart. A live-only lookup would report both cases
    # identically as "no match".
    match = next((c for c in candidates if c.run_dir.name == run_id), None)
    if match is None:
        # A root the NFS lock-ownership gate refused is excluded from
        # candidates entirely, so "no match" here can genuinely mean
        # "the id lives on a root we were never allowed to look at" - surface
        # that instead of letting it read identically to "no such id anywhere".
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
        console.print(f"[red]no run matching {run_id!r} found {scope_desc}[/].")
        raise typer.Exit(code=1)
    if match.run_dir not in live_run_dirs:
        console.print(f"[red]run {run_id} is not currently live[/].")
        raise typer.Exit(code=1)
    if confirm is not None and not confirm(match):
        console.print(f"[red]not stopping {run_id}[/].")
        raise typer.Exit(code=1)
    grace_seconds = timeout if timeout is not None else match.cfg.stop_grace_seconds
    return build_stop.stop_run(match.run_dir, match.cfg, force=force, grace_seconds=grace_seconds)


def _host_wide_candidates() -> tuple[list[build_stop.RunCandidate], list[build_stop.SkippedRoot]]:
    """Scan every live, host-mode build on this host, not just one workspace.

    Calls :func:`build_stop._discover_host_cookers` to find every topdir
    carrying a running bitbake cooker anywhere on the host, then re-scans
    each topdir's own ``runs/`` directory directly via
    :func:`build_stop.enumerate_workspace_runs` - never
    :func:`build_stop.correlate_host_discoveries`, which internally discards
    the skipped-root list this helper needs in order to surface a peer-held
    root's ownership warning.

    Every topdir's :attr:`build_stop.RunScan.skipped` entries are combined
    into one ``skipped`` list, and every topdir's candidates that are both
    live (:func:`build_stop.is_candidate_live`) and host-mode
    (``read_launch_record(...).mode == "host"``) are combined into one
    ``candidates`` list. The mode filter is required, not optional: a single
    topdir's ``runs/`` can hold both a host-mode and a live container-mode
    run, and ``enumerate_workspace_runs`` does not itself filter by mode.
    """
    discovered = build_stop._discover_host_cookers()
    candidates: list[build_stop.RunCandidate] = []
    skipped: list[build_stop.SkippedRoot] = []
    for topdir in discovered:
        scan = build_stop.enumerate_workspace_runs(topdir / "runs", user_config=_state._USER_CONFIG)
        skipped.extend(scan.skipped)
        for candidate in scan.candidates:
            if not build_stop.is_candidate_live(candidate):
                continue
            if build_stop.read_launch_record(candidate.run_dir).mode != "host":
                continue
            candidates.append(candidate)
    return candidates, skipped


def _host_wide_fallback_applies(workspace: Path | None, family: str | None, kas_yaml: Path | None) -> bool:
    """True when a bare, no-workspace invocation should search the whole host.

    All three must hold: no explicit ``--workspace`` was passed, this is not
    the generic BYO carve-out (``family == "generic" and kas_yaml is not
    None``, which resolves its own workspace from the YAML's own location and
    therefore never needs the host-wide fallback), and the cwd walk
    (:func:`_find_workspace_from_cwd`) finds no workspace either.
    """
    if workspace is not None:
        return False
    if family == "generic" and kas_yaml is not None:
        return False
    return _find_workspace_from_cwd() is None


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

    # No workspace signal at all (no --workspace, not the generic BYO
    # carve-out, and the cwd walk finds nothing) plus an explicit --run: search
    # every live host-mode build on this host rather than exiting with "Not
    # inside a BSP workspace" for an operator who has simply lost track of
    # which directory they are in. Checked ahead of `_resolve_workspace`
    # because that call itself exits 2 on exactly this condition - it must
    # never run on this branch.
    if _host_wide_fallback_applies(workspace, family, kas_yaml) and run_id is not None:
        candidates, skipped = _host_wide_candidates()
        stopped = _stop_matched_run(
            run_id,
            candidates=candidates,
            live_run_dirs={c.run_dir for c in candidates},
            skipped=skipped,
            scope_desc="among live builds on this host",
            confirm=(None if force else _confirm_host_wide),
            force=force,
            timeout=timeout,
        )
        if not stopped:
            raise typer.Exit(code=1)
        return

    ws = _resolve_workspace(workspace, kas_yaml=kas_yaml, family=family)

    if run_id is not None:
        scan = build_stop.enumerate_workspace_runs(ws, user_config=_state._USER_CONFIG)
        skipped = scan.skipped
        # live_workspace_runs now verifies container-mode liveness against the
        # runtime itself (not just "a launch record with a label exists"), so a
        # single membership check in _stop_matched_run answers both host- and
        # container-mode targets correctly - no separate runtime query needed
        # at this call site.
        live_run_dirs = {c.run_dir for c in build_stop.live_workspace_runs(ws, user_config=_state._USER_CONFIG)}
        stopped = _stop_matched_run(
            run_id,
            candidates=scan.candidates,
            live_run_dirs=live_run_dirs,
            skipped=skipped,
            force=force,
            timeout=timeout,
        )
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
    scan = build_stop.enumerate_workspace_runs(ws, user_config=_state._USER_CONFIG)
    skipped = scan.skipped
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
            stopped = _stop_matched_run(
                chosen_run_id,
                candidates=scan.candidates,
                live_run_dirs={c.run_dir for c in live},
                skipped=skipped,
                force=force,
                timeout=timeout,
            )
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
