"""bakar prserv subcommand - lifecycle for the workspace PR-service daemon.

Mirrors ``bakar hashserv``: three verbs (``start``, ``stop``, ``status``) drive
the :mod:`bakar.prserv` module against the current workspace. Workspace
resolution matches the no-manifest read-only commands - each verb accepts
``--workspace/-w`` and otherwise walks up from CWD. The daemon binds
``cluster_bind_host`` (config) so other cluster nodes can reach the PR service;
unset means localhost-only.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

import bakar.commands._app as _state
from bakar import prserv
from bakar.commands._app import app, console
from bakar.commands._helpers import _dispatch_bsp, _dispatch_from_yaml, _resolve_workspace
from bakar.config import BuildConfig, resolve

prserv_app = typer.Typer(
    help="Manage the workspace bitbake-prserv daemon (start/stop/status).",
    no_args_is_help=True,
)


def _resolve_cfg(workspace: Path | None = None, kas_yaml: Path | None = None) -> BuildConfig:
    """Resolve the :class:`BuildConfig` for the current workspace (see bakar hashserv)."""
    if kas_yaml is not None:
        family, _bsp = _dispatch_from_yaml(kas_yaml)
    else:
        family, _bsp = _dispatch_bsp(None)
    ws = _resolve_workspace(workspace, kas_yaml=kas_yaml, family=family)
    return resolve(
        workspace=ws,
        bsp_family=family,
        kas_yaml=kas_yaml,
        user_config=_state._USER_CONFIG,
    )


def _bind_host(cfg: BuildConfig) -> str:
    return cfg.cluster_bind_host or "localhost"


@prserv_app.command("start")
def start(
    kas_yaml: Annotated[
        Path | None,
        typer.Argument(exists=False, help="Optional kas YAML; routes through _dispatch_from_yaml"),
    ] = None,
    workspace: Annotated[
        Path | None,
        typer.Option("--workspace", "-w", help="Workspace root; auto-detected if omitted"),
    ] = None,
) -> None:
    """Start the workspace prserv daemon (or report the existing PRSERV_HOST)."""
    cfg = _resolve_cfg(workspace, kas_yaml)
    addr = prserv.ensure_running(cfg.prserv_state_key, binary_root=cfg.bsp_root, bind_host=_bind_host(cfg))
    if addr is None:
        console.print(
            "failed to start prserv: bitbake-prserv not found or startup probe failed; "
            f"see {cfg.prserv_state_key}/.bakar/prserv.stderr"
        )
        raise typer.Exit(code=1)
    console.print(f"started: PRSERV_HOST={addr}")


@prserv_app.command("stop")
def stop(
    kas_yaml: Annotated[
        Path | None,
        typer.Argument(exists=False, help="Optional kas YAML; routes through _dispatch_from_yaml"),
    ] = None,
    workspace: Annotated[
        Path | None,
        typer.Option("--workspace", "-w", help="Workspace root; auto-detected if omitted"),
    ] = None,
) -> None:
    """Gracefully stop the workspace prserv daemon (preserves the PR DB)."""
    cfg = _resolve_cfg(workspace, kas_yaml)
    if prserv.stop(cfg.prserv_state_key, binary_root=cfg.bsp_root, bind_host=_bind_host(cfg)):
        console.print("stopped")
    else:
        console.print("not running")


@prserv_app.command("status")
def status(
    kas_yaml: Annotated[
        Path | None,
        typer.Argument(exists=False, help="Optional kas YAML; routes through _dispatch_from_yaml"),
    ] = None,
    workspace: Annotated[
        Path | None,
        typer.Option("--workspace", "-w", help="Workspace root; auto-detected if omitted"),
    ] = None,
) -> None:
    """Print the current daemon state (PRSERV_HOST, or ``not running``)."""
    cfg = _resolve_cfg(workspace, kas_yaml)
    host = _bind_host(cfg)
    if prserv.is_running(cfg.prserv_state_key, bind_host=host):
        port = prserv._workspace_port(cfg.prserv_state_key)
        console.print(f"running, PRSERV_HOST={host}:{port}")
    else:
        console.print("not running")


app.add_typer(prserv_app, name="prserv")
