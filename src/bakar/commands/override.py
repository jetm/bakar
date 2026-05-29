"""bspctl bitbake-override subcommand - swap the BSP-bundled bitbake."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import bspctl.commands._app as _state
import typer
from bspctl.commands._app import app, console
from bspctl.commands._helpers import _dispatch_bsp, _workspace_from_cwd
from bspctl.config import resolve
from bspctl.observability import RunLogger
from bspctl.steps import bitbake_override as step_override


@app.command("bitbake-override")
def bitbake_override_cmd(
    apply_flag: Annotated[
        bool,
        typer.Option("--apply", help="Apply the override (default action)."),
    ] = False,
    revert_flag: Annotated[
        bool,
        typer.Option("--revert", help="Remove the symlink; next build's repo sync restores the BSP bitbake."),
    ] = False,
    status_flag: Annotated[
        bool,
        typer.Option("--status", help="Print current override state and exit."),
    ] = False,
    branch: Annotated[
        str | None,
        typer.Option(
            "--branch",
            help="Override branch in the upstream bitbake repo. "
            "Defaults to br-<major>.<minor> auto-detected from the BSP's bundled bitbake.",
        ),
    ] = None,
    repo: Annotated[
        Path | None,
        typer.Option(
            "--repo",
            help="Path to the upstream bitbake source repo. "
            "Defaults to ~/repos/personal/yocto/bitbake or BSPCTL_BITBAKE_OVERRIDE_REPO.",
        ),
    ] = None,
    manifest: Annotated[
        str | None,
        typer.Option(
            "--manifest",
            "-f",
            help="Manifest filename used to dispatch BSP family. Defaults to BSPCTL_MANIFEST or the NXP default.",
        ),
    ] = None,
    workspace: Annotated[Path | None, typer.Option("--workspace", "-w", help="Workspace root override")] = None,
) -> None:
    """Swap the BSP-bundled bitbake for a symlink to a local upstream checkout.

    Default action with no flags is --status. Use --apply to swap, --revert
    to remove the symlink (next ``bspctl build`` re-syncs to restore the BSP
    bitbake). The override is auto-applied as part of ``bspctl build``.

    The path swapped depends on the dispatched BSP family: NXP swaps
    ``nxp/sources/poky/bitbake``; TI swaps ``ti/sources/bitbake``. Pass
    ``--manifest`` (or set ``BSPCTL_MANIFEST``) to target TI; without it
    the command defaults to the NXP family.
    """
    selected = sum(1 for f in (apply_flag, revert_flag, status_flag) if f)
    if selected > 1:
        console.print("[red]choose at most one of --apply / --revert / --status[/]")
        raise typer.Exit(code=2)

    ws = workspace or _workspace_from_cwd()
    family, _bsp = _dispatch_bsp(manifest)
    cfg = resolve(workspace=ws, bsp_family=family, manifest=manifest, user_config=_state._USER_CONFIG)
    cfg.runs_dir.mkdir(parents=True, exist_ok=True)

    if revert_flag:
        with RunLogger(runs_dir=cfg.runs_dir) as log:
            step_override.revert(cfg, log)
        st = step_override.status(cfg)
        console.print(f"[yellow]reverted[/]: {st.detail}")
        return

    if apply_flag:
        with RunLogger(runs_dir=cfg.runs_dir) as log:
            try:
                result = step_override.apply(cfg, log, branch=branch, repo_path=repo)
            except RuntimeError as exc:
                console.print(f"[red]bitbake-override failed[/]: {exc}")
                raise typer.Exit(code=2) from exc
        _print_override_status(result)
        return

    st = step_override.status(cfg)
    _print_override_status(st)


def _print_override_status(st) -> None:
    state_colour = {
        "active": "green",
        "stale": "yellow",
        "disabled": "dim",
        "missing": "dim",
    }.get(st.state, "white")
    bits: list[str] = [f"[{state_colour}]{st.state}[/]"]
    if st.branch:
        bits.append(f"branch={st.branch}")
    if st.sha:
        bits.append(f"sha={st.sha}")
    if st.upstream_version:
        bits.append(f"upstream={st.upstream_version}")
    if st.bsp_version:
        bits.append(f"bsp={st.bsp_version}")
    bits.append(f"({st.detail})")
    console.print("bitbake-override: " + " ".join(bits))
