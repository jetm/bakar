"""bakar clean subcommand - wipe the BSP build directory."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

import typer

import bakar.commands._app as _state
from bakar.commands._app import app, console
from bakar.commands._helpers import (
    WorkspaceOption,
    _bsp_from_cwd,
    _clean_build_dir,
    _dispatch_bsp,
    _dispatch_from_yaml,
    _resolve_workspace,
    _workspace_from_cwd,
    split_kas_yaml_arg,
)
from bakar.config import resolve


def _resolve_family(
    bsp: str | None,
    manifest: str | None,
    ws: Path,
) -> Literal["nxp", "ti"]:
    """Resolve the BSP family from clean's flag ladder.

    Order: explicit ``--bsp`` value (validated against ``nxp``/``ti``); the
    ``--manifest`` alias dispatched through :func:`_dispatch_bsp`; cwd
    auto-detection via :func:`_bsp_from_cwd`. Any unresolvable path raises
    ``typer.Exit(code=2)`` with the appropriate hint - matching the prior
    inline behavior so callers do not need to special-case None.
    """
    if bsp is not None:
        if bsp not in ("nxp", "ti"):
            console.print(f"[red]invalid --bsp value[/]: {bsp!r} (expected 'nxp' or 'ti')")
            raise typer.Exit(code=2)
        return bsp  # type: ignore[return-value]
    if manifest is not None:
        family, _bsp_model = _dispatch_bsp(manifest)
        return family
    family = _bsp_from_cwd(ws)
    if family is None:
        console.print("[red]could not auto-detect BSP from cwd. Pass --bsp nxp|ti or --manifest <file>.[/]")
        raise typer.Exit(code=2)
    return family


@app.command()
def clean(
    kas_yaml: Annotated[
        str | None,
        typer.Argument(
            help="BYO kas YAML (e.g. meta-avocado/kas/machine/qemuarm64.yml). When given, "
            "clean that build dir (workspace/build-<stem>) instead of an nxp/ti BSP dir.",
        ),
    ] = None,
    all: Annotated[bool, typer.Option("--all", help="Also remove the generated kas YAML")] = False,
    bsp: Annotated[
        str | None,
        typer.Option("--bsp", help="BSP family to clean: 'nxp' or 'ti'. Auto-detected from cwd if omitted."),
    ] = None,
    manifest: Annotated[
        str | None,
        typer.Option("--manifest", "-f", help="Manifest filename (back-compat alias for --bsp)"),
    ] = None,
    workspace: WorkspaceOption = None,
) -> None:
    """Remove the build/ directory. Use --all to also drop the kas YAML.

    Pass a kas YAML positionally to clean a BYO/meta-avocado build dir
    (``workspace/build-<yaml-stem>/build``), mirroring ``bakar build my.yml``;
    otherwise the nxp/ti BSP build dir is cleaned.
    """
    if kas_yaml is not None:
        # BYO/generic form: resolve the build dir from the YAML exactly as
        # `bakar build my.yml` does, so a meta-avocado machine build dir is
        # reachable (the --bsp ladder only expresses nxp/ti).
        main_yaml, _extras = split_kas_yaml_arg(kas_yaml)
        family, _bsp = _dispatch_from_yaml(main_yaml)
        ws = _resolve_workspace(workspace, kas_yaml=main_yaml, family=family)
        cfg = resolve(workspace=ws, bsp_family=family, kas_yaml=main_yaml, user_config=_state._USER_CONFIG)
    else:
        ws = workspace or _workspace_from_cwd()
        family = _resolve_family(bsp, manifest, ws)
        cfg = resolve(workspace=ws, bsp_family=family, user_config=_state._USER_CONFIG)
    if all and cfg.hashserv_state_key == cfg.bsp_root:
        # Stop the hashserv daemon before wiping, but only when it is keyed to
        # this workspace (the no-shared-sstate fallback). When the daemon is
        # keyed to a shared SSTATE_DIR, sibling workspaces depend on it and its
        # DB lives outside this build dir, so wiping the dir leaves it valid -
        # stopping it here would disrupt an unrelated workspace's build. Lazy
        # import to avoid any future import cycle if hashserv grows deps.
        from bakar import hashserv

        hashserv.stop(cfg.hashserv_state_key)
    _clean_build_dir(cfg)
    if all and cfg.kas_yaml.exists():
        cfg.kas_yaml.unlink()
        console.print(f"[green]removed[/] {cfg.kas_yaml}")
