"""bspctl hashserv subcommand - lifecycle for the workspace hashserv daemon.

The sub-app exposes three verbs (``start``, ``stop``, ``status``) that drive the
``bspctl.hashserv`` module against the current workspace. Workspace resolution
mirrors the no-manifest read-only commands (``layers``, ``for-all``): each verb
accepts ``--workspace/-w`` and falls back to walking up from CWD via
``_resolve_workspace``; dispatch through ``_dispatch_bsp(None)`` so the same
NXP/TI default applies as elsewhere.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import bspctl.commands._app as _state
import typer
from bspctl import hashserv
from bspctl.commands._app import app, console
from bspctl.commands._helpers import _dispatch_bsp, _dispatch_from_yaml, _resolve_workspace
from bspctl.config import resolve

hashserv_app = typer.Typer(
    help="Manage the workspace bitbake-hashserv daemon (start/stop/status).",
    no_args_is_help=True,
)


def _resolve_bsp_root(workspace: Path | None = None, kas_yaml: Path | None = None) -> Path:
    """Resolve the per-family BSP root for the current workspace.

    Mirrors the doctor command's no-manifest path: walk up from CWD (or
    use the explicit ``workspace`` when supplied) to find the workspace,
    dispatch the BSP family (defaulting to NXP when no manifest argument
    or env var is set), and return the resolved ``BuildConfig.bsp_root``.

    When ``kas_yaml`` is supplied, the BSP family is inferred from the
    YAML via :func:`_dispatch_from_yaml` instead of the NXP-default
    manifest dispatch. ``_resolve_workspace`` then routes through the
    generic-mode carve-out so generic kas YAMLs work from any directory
    without ``--workspace``.
    """
    if kas_yaml is not None:
        family, _bsp = _dispatch_from_yaml(kas_yaml)
    else:
        family, _bsp = _dispatch_bsp(None)
    ws = _resolve_workspace(workspace, kas_yaml=kas_yaml, family=family)
    cfg = resolve(
        workspace=ws,
        bsp_family=family,
        kas_yaml=kas_yaml,
        user_config=_state._USER_CONFIG,
    )
    return cfg.bsp_root


@hashserv_app.command("start")
def start(
    kas_yaml: Annotated[
        Path | None,
        typer.Argument(
            exists=False,
            help="Optional kas YAML; routes through _dispatch_from_yaml when supplied",
        ),
    ] = None,
    workspace: Annotated[
        Path | None,
        typer.Option("--workspace", "-w", help="Workspace root; auto-detected if omitted"),
    ] = None,
) -> None:
    """Start the workspace hashserv daemon (or report the existing URL)."""
    bsp_root = _resolve_bsp_root(workspace, kas_yaml)
    url = hashserv.ensure_running(bsp_root)
    if url is None:
        console.print(
            f"failed to start hashserv: bitbake-hashserv not found or startup probe failed; "
            f"see {bsp_root}/.bspctl/hashserv.stderr"
        )
        raise typer.Exit(code=1)
    console.print(f"started: {url}")


@hashserv_app.command("stop")
def stop(
    kas_yaml: Annotated[
        Path | None,
        typer.Argument(
            exists=False,
            help="Optional kas YAML; routes through _dispatch_from_yaml when supplied",
        ),
    ] = None,
    workspace: Annotated[
        Path | None,
        typer.Option("--workspace", "-w", help="Workspace root; auto-detected if omitted"),
    ] = None,
) -> None:
    """Signal the workspace hashserv daemon to stop and clean PID/port files."""
    bsp_root = _resolve_bsp_root(workspace, kas_yaml)
    if hashserv.stop(bsp_root):
        console.print("stopped")
    else:
        console.print("not running")


@hashserv_app.command("status")
def status(
    kas_yaml: Annotated[
        Path | None,
        typer.Argument(
            exists=False,
            help="Optional kas YAML; routes through _dispatch_from_yaml when supplied",
        ),
    ] = None,
    workspace: Annotated[
        Path | None,
        typer.Option("--workspace", "-w", help="Workspace root; auto-detected if omitted"),
    ] = None,
) -> None:
    """Print the current daemon state (URL + PID, or ``not running``)."""
    bsp_root = _resolve_bsp_root(workspace, kas_yaml)
    if not hashserv.is_running(bsp_root):
        console.print("not running")
        return
    state_dir = bsp_root / ".bspctl"
    try:
        pid = (state_dir / "hashserv.pid").read_text().strip()
        port = (state_dir / "hashserv.port").read_text().strip()
    except OSError:
        # Race: daemon exited between is_running() and the file reads.
        console.print("not running")
        return
    console.print(f"running, pid={pid}, url=ws://localhost:{port}")


app.add_typer(hashserv_app, name="hashserv")
