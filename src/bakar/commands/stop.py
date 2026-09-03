"""bakar stop subcommand - gracefully halt a running build."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

import bakar.commands._app as _state
from bakar import build_stop
from bakar.commands._app import app
from bakar.commands._helpers import (
    WorkspaceOption,
    _normalize_dispatch,
    _resolve_workspace,
    split_kas_yaml_arg,
)
from bakar.config import BSPSpec, resolve
from bakar.steps import remote_dispatch

# Matches config.py's stop_grace_seconds default. A remote stop resolves no
# BuildConfig - the build is on the other host - so the configured value is not
# reachable here and the default has to be restated.
_REMOTE_STOP_GRACE_SECONDS = 30


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
    cfg = resolve(
        workspace=ws,
        bsp_family=family,
        spec=BSPSpec(manifest=manifest),
        kas_yaml=kas_yaml,
        user_config=_state._USER_CONFIG,
    )
    grace_seconds = timeout if timeout is not None else cfg.stop_grace_seconds
    stopped = build_stop.stop_build(cfg.bsp_root, cfg, force=force, grace_seconds=grace_seconds)
    if not stopped:
        raise typer.Exit(code=1)
