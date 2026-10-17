"""bakar hashserv subcommand - lifecycle for the workspace hashserv daemon.

The sub-app exposes three verbs (``start``, ``stop``, ``status``) that drive the
``bakar.hashserv`` module against the current workspace. Workspace resolution
mirrors the no-manifest read-only commands (``layers``, ``for-all``): each verb
accepts ``--workspace/-w`` and falls back to walking up from CWD via
``_resolve_workspace``; dispatch through ``_dispatch_bsp(None)`` so the same
NXP/TI default applies as elsewhere.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.markup import escape

from bakar import hashserv
from bakar.commands._app import app, console
from bakar.commands._helpers import WorkspaceOption
from bakar.commands._workspace import _resolve_daemon_cfg
from bakar.hashserv import NetworkStateDirError

hashserv_app = typer.Typer(
    help="Manage the workspace bitbake-hashserv daemon (start/stop/status).",
    no_args_is_help=True,
)


@hashserv_app.command("start")
def start(
    kas_yaml: Annotated[
        Path | None,
        typer.Argument(
            exists=False,
            help="Optional kas YAML; routes through _dispatch_from_yaml when supplied",
        ),
    ] = None,
    workspace: WorkspaceOption = None,
) -> None:
    """Start the workspace hashserv daemon (or report the existing URL)."""
    cfg = _resolve_daemon_cfg(workspace, kas_yaml)
    try:
        url = hashserv.ensure_running(
            cfg.hashserv_state_key,
            binary_root=cfg.bsp_root,
            bind_host=cfg.cluster_bind_host or "localhost",
        )
    except NetworkStateDirError as exc:
        console.print(f"[red]{escape(str(exc))}[/]")
        raise typer.Exit(code=1) from exc
    if url is None:
        console.print(
            f"failed to start hashserv: bitbake-hashserv not found or startup probe failed; "
            f"see {cfg.hashserv_state_key}/.bakar/hashserv.stderr"
        )
        raise typer.Exit(code=1)
    typer.echo(f"started: {url}")


@hashserv_app.command("stop")
def stop(
    kas_yaml: Annotated[
        Path | None,
        typer.Argument(
            exists=False,
            help="Optional kas YAML; routes through _dispatch_from_yaml when supplied",
        ),
    ] = None,
    workspace: WorkspaceOption = None,
) -> None:
    """Signal the workspace hashserv daemon to stop and clean PID/port files."""
    cfg = _resolve_daemon_cfg(workspace, kas_yaml)
    if hashserv.stop(cfg.hashserv_state_key):
        typer.echo("stopped")
    else:
        typer.echo("not running")


@hashserv_app.command("status")
def status(
    kas_yaml: Annotated[
        Path | None,
        typer.Argument(
            exists=False,
            help="Optional kas YAML; routes through _dispatch_from_yaml when supplied",
        ),
    ] = None,
    workspace: WorkspaceOption = None,
) -> None:
    """Print the current daemon state (URL + PID, or ``not running``)."""
    cfg = _resolve_daemon_cfg(workspace, kas_yaml)
    if not hashserv.is_running(cfg.hashserv_state_key):
        typer.echo("not running")
        return
    state = hashserv.read_state(cfg.hashserv_state_key)
    if state is None:
        # Race: daemon exited between is_running() and the file reads.
        typer.echo("not running")
        return
    pid, port = state
    typer.echo(f"running, pid={pid}, url={hashserv.bound_url(port, cfg.cluster_bind_host)}")


app.add_typer(hashserv_app, name="hashserv")
