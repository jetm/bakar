"""bakar getvar subcommand - variable resolution and provenance.

Runs ``bitbake-getvar <VAR>`` (no recipe) or ``bitbake-getvar -r <recipe> <VAR>``
inside kas-container to resolve a BitBake variable. With ``--history``, runs
``bitbake -e`` instead and extracts the include-chain source locations via
:func:`bakar.inspect_parse.extract_var_history`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

import bakar.commands._app as _state
from bakar.commands._app import app, console
from bakar.commands._helpers import (
    _dispatch_bsp,
    _overlay_for,
    _resolve_workspace,
)
from bakar.config import BSPSpec, resolve
from bakar.inspect_parse import extract_var_history
from bakar.observability import RunLogger
from bakar.steps.kas_build import KasBuildContext, run_shell_capture


@app.command("getvar")
def getvar(
    var: Annotated[
        str,
        typer.Argument(help="BitBake variable name to resolve (e.g. MACHINE, IMAGE_INSTALL)."),
    ],
    recipe: Annotated[
        str | None,
        typer.Option("--recipe", "-r", help="Resolve the variable within this recipe's parse context."),
    ] = None,
    unexpanded: Annotated[
        bool,
        typer.Option(
            "--unexpanded",
            "-u",
            help="Print the value before ${...} expansion (passed to bitbake-getvar as -e).",
        ),
    ] = False,
    history: Annotated[
        bool,
        typer.Option(
            "--history",
            help="Show where the variable was set across the include chain (uses bitbake -e).",
        ),
    ] = False,
    manifest: Annotated[
        str | None,
        typer.Option("--manifest", "-f", help="Manifest filename used to dispatch BSP family"),
    ] = None,
    machine: Annotated[
        str | None,
        typer.Option("--machine", "-m", help="Override the target machine"),
    ] = None,
    workspace: Annotated[
        Path | None,
        typer.Option("--workspace", "-w", help="Workspace root override"),
    ] = None,
    output_json: Annotated[
        bool,
        typer.Option("--json", help="Emit a JSON document with keys var, recipe, value/history."),
    ] = False,
) -> None:
    """Resolve a BitBake variable inside kas-container.

    Without ``--recipe``, runs ``bitbake-getvar <VAR>`` (global context).
    With ``--recipe``, scopes to that recipe's parse context.

    ``--unexpanded`` prints the value before ``${...}`` substitution by
    passing the ``-e`` flag to ``bitbake-getvar``.

    ``--history`` uses ``bitbake -e`` to capture the full include-chain
    history and shows the ordered list of ``file:line`` source locations
    where the variable was set or appended. Prints ``no history recorded``
    and exits 0 when no history comments are present.

    Exits non-zero when the underlying bitbake call fails. Empty output
    from a failing bitbake call is surfaced as an error rather than printed
    as success.
    """
    family, bsp = _dispatch_bsp(manifest)
    ws = _resolve_workspace(workspace, family=family)
    cfg = resolve(
        workspace=ws,
        bsp_family=family,
        spec=BSPSpec(manifest=manifest, machine=machine),
        user_config=_state._USER_CONFIG,
    )
    overlay_source = _overlay_for(bsp)
    cfg.runs_dir.mkdir(parents=True, exist_ok=True)

    with RunLogger(runs_dir=cfg.runs_dir) as log:
        kas_ctx = KasBuildContext(cfg, log, cfg.kas_yaml, overlay_source)

        if history:
            _run_history(kas_ctx, log, var, recipe, output_json)
        else:
            _run_getvar(kas_ctx, log, var, recipe, unexpanded, output_json)


def _run_getvar(
    kas_ctx: KasBuildContext,
    log: RunLogger,
    var: str,
    recipe: str | None,
    unexpanded: bool,
    output_json: bool,
) -> None:
    """Run ``bitbake-getvar`` and print the result."""
    # Build the bitbake-getvar command.
    # -e flag: print unexpanded value.
    # -r <recipe>: scope to recipe parse context.
    parts = ["bitbake-getvar"]
    if unexpanded:
        parts.append("-e")
    if recipe:
        parts += ["-r", recipe]
    parts.append(var)
    command = " ".join(parts)

    capture_path = log.run_dir / f"getvar-{var}.log"
    rc = run_shell_capture(kas_ctx, command, capture_path, step="getvar")

    raw = capture_path.read_text(errors="replace") if capture_path.exists() else ""

    if rc != 0:
        console.print(f"[red]bitbake-getvar failed (exit {rc}).[/]")
        if raw.strip():
            console.print(raw)
        raise typer.Exit(code=rc)

    # Extract the value from bitbake-getvar output.
    # bitbake-getvar emits lines like:
    #   # $MACHINE
    #   #   set /path/to/local.conf:5
    #   MACHINE="imx8mp-lpddr4-evk"
    value = _parse_getvar_value(raw, var)

    if output_json:
        doc: dict = {"var": var, "value": value}
        if recipe:
            doc["recipe"] = recipe
        typer.echo(json.dumps(doc, indent=2))
    else:
        console.print(value, highlight=False)


def _run_history(
    kas_ctx: KasBuildContext,
    log: RunLogger,
    var: str,
    recipe: str | None,
    output_json: bool,
) -> None:
    """Run ``bitbake -e`` and extract the variable's include-chain history."""
    parts = ["bitbake", "-e"]
    if recipe:
        parts.append(recipe)
    command = " ".join(parts)

    capture_path = log.run_dir / f"getvar-history-{var}.log"
    rc = run_shell_capture(kas_ctx, command, capture_path, step="getvar_history")

    env_text = capture_path.read_text(errors="replace") if capture_path.exists() else ""

    if rc != 0:
        console.print(f"[red]bitbake -e failed (exit {rc}).[/]")
        if env_text.strip():
            console.print(env_text)
        raise typer.Exit(code=rc)

    locations = extract_var_history(env_text, var)

    if output_json:
        doc: dict = {"var": var, "history": locations}
        if recipe:
            doc["recipe"] = recipe
        typer.echo(json.dumps(doc, indent=2))
        return

    if not locations:
        console.print("no history recorded", highlight=False)
    else:
        console.print(f"[bold]{var}[/] history (include-chain order):")
        for loc in locations:
            console.print(f"  {loc}", highlight=False)


def _parse_getvar_value(raw: str, var: str) -> str:
    """Extract the variable value from ``bitbake-getvar`` output.

    ``bitbake-getvar`` emits text of the form::

        # $MACHINE
        #   set /path/to/local.conf:5
        MACHINE="imx8mp-lpddr4-evk"

    Returns the unquoted value string, or the full raw output when the
    expected ``VAR="..."`` line is not found (e.g. the variable is unset).
    """
    for line in raw.splitlines():
        stripped = line.strip()
        prefix = f'{var}="'
        if stripped.startswith(prefix) and stripped.endswith('"'):
            return stripped[len(prefix):-1].replace('\\"', '"')
    # Fallback: return stripped raw output so the caller always gets something.
    return raw.strip()
