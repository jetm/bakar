"""bakar getvar subcommand - variable resolution and provenance.

Runs ``bitbake-getvar <VAR>`` (no recipe) or ``bitbake-getvar -r <recipe> <VAR>``
inside kas-container to resolve a BitBake variable. With ``--history``, runs
``bitbake -e`` instead and extracts the include-chain source locations via
:func:`bakar.inspect_parse.extract_var_history`.
"""

from __future__ import annotations

import json
import shlex
from typing import Annotated

import typer

import bakar.commands._app as _state
from bakar.commands._app import app, console
from bakar.commands._helpers import (
    WorkspaceOption,
    _combine_overlays_with_tuning,
    _normalize_dispatch,
    _overlay_for,
    _resolve_workspace,
    apply_mold_overrides,
    apply_sccache_overrides,
    global_container_mode,
    global_host_mode,
    split_kas_yaml_arg,
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
    kas_yaml: Annotated[
        str | None,
        typer.Argument(
            help="Optional kas YAML (BYO/bbsetup); supports colon-overlay syntax: machine.yml:overlay.yml.",
        ),
    ] = None,
    recipe: Annotated[
        str | None,
        typer.Option("--recipe", "-r", help="Resolve the variable within this recipe's parse context."),
    ] = None,
    unexpanded: Annotated[
        bool,
        typer.Option(
            "--unexpanded",
            "-u",
            help="Print the value before ${...} expansion (passed to bitbake-getvar as -u).",
        ),
    ] = False,
    flag: Annotated[
        str | None,
        typer.Option(
            "--flag",
            help="Resolve a variable flag instead of the variable itself; equivalent to VAR[FLAG].",
        ),
    ] = None,
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
    workspace: WorkspaceOption = None,
    output_json: Annotated[
        bool,
        typer.Option("--json", help="Emit a JSON document with keys var, recipe, value/history."),
    ] = False,
) -> None:
    """Resolve a BitBake variable inside kas-container.

    Without ``--recipe``, runs ``bitbake-getvar <VAR>`` (global context).
    With ``--recipe``, scopes to that recipe's parse context.

    ``--unexpanded`` prints the value before ``${...}`` substitution by
    passing the ``-u`` flag to ``bitbake-getvar``.

    Flag-valued variables are queried either with ``--flag FLAG`` or with
    the inline spelling ``VAR[FLAG]``; both normalise to the same
    ``bitbake-getvar -f FLAG VAR`` call. Supplying both at once exits 2.
    Note that bakar's ``-f`` remains ``--manifest``; ``--flag`` is
    long-only.

    ``--history`` uses ``bitbake -e`` to capture the full include-chain
    history and shows the ordered list of ``file:line`` source locations
    where the variable was set or appended. Prints ``no history recorded``
    and exits 0 when no history comments are present.

    Exits non-zero when the underlying bitbake call fails. Empty output
    from a failing bitbake call is surfaced as an error rather than printed
    as success.
    """
    var, flag = _normalize_flag_query(var, flag)

    # ``--history`` reads the include chain out of ``bitbake -e``, which
    # records variable assignments and has no per-flag history. Refuse the
    # combination rather than silently answering for the bare name.
    if flag and history:
        console.print("[red]--history cannot be combined with a variable flag query[/]")
        raise typer.Exit(code=2)

    main_yaml, user_extras = split_kas_yaml_arg(kas_yaml)
    family, bsp, main_yaml, manifest = _normalize_dispatch(main_yaml, manifest)
    ws = _resolve_workspace(workspace, kas_yaml=main_yaml, family=family)
    cfg = resolve(
        workspace=ws,
        bsp_family=family,
        spec=BSPSpec(
            manifest=manifest, machine=machine, host_mode=global_host_mode(), container_mode=global_container_mode()
        ),
        kas_yaml=main_yaml,
        user_config=_state._USER_CONFIG,
    )
    cfg = apply_sccache_overrides(cfg)
    cfg = apply_mold_overrides(cfg)
    overlay_source = _overlay_for(bsp)
    extra_overlays = _combine_overlays_with_tuning(user_extras, cfg)
    cfg.runs_dir.mkdir(parents=True, exist_ok=True)

    with RunLogger(runs_dir=cfg.runs_dir) as log:
        kas_ctx = KasBuildContext(cfg, log, cfg.kas_yaml, overlay_source, extra_overlays=extra_overlays)

        if history:
            _run_history(kas_ctx, log, var, recipe, output_json)
        else:
            _run_getvar(kas_ctx, log, var, recipe, unexpanded, output_json, flag)


def _normalize_flag_query(var: str, flag: str | None) -> tuple[str, str | None]:
    """Fold the inline ``VAR[FLAG]`` spelling into the ``--flag`` form.

    Both spellings must reach :func:`_run_getvar` as the same
    ``(name, flag)`` pair so there is only one query path. Supplying both
    at once is ambiguous rather than redundant - ``FOO[a] --flag b`` has no
    sensible reading - so it exits 2.
    """
    if "[" not in var:
        return var, flag

    name, _, bracketed = var.partition("[")
    inline_flag = bracketed.removesuffix("]") if bracketed.endswith("]") else ""
    if not name or not inline_flag:
        console.print(f"[red]malformed variable flag syntax:[/] {var} (expected VAR[FLAG])")
        raise typer.Exit(code=2)

    if flag is not None:
        console.print("[red]pass either --flag or the inline VAR[FLAG] form, not both[/]")
        raise typer.Exit(code=2)

    return name, inline_flag


def _run_getvar(
    kas_ctx: KasBuildContext,
    log: RunLogger,
    var: str,
    recipe: str | None,
    unexpanded: bool,
    output_json: bool,
    flag: str | None = None,
) -> None:
    """Run ``bitbake-getvar`` and print the result."""
    # Build the bitbake-getvar command.
    # --value: print the bare value with no provenance comment block.
    # --ignore-undefined: an unset variable is an empty value, not an error.
    # -u: print unexpanded value (bitbake rejects it without --value).
    # -r <recipe>: scope to recipe parse context.
    # -f <flag>: read a variable flag (bitbake's own short option, unrelated
    #   to bakar's -f/--manifest).
    parts = ["bitbake-getvar", "--value", "--ignore-undefined"]
    if unexpanded:
        parts.append("-u")
    if flag:
        parts += ["-f", shlex.quote(flag)]
    if recipe:
        parts += ["-r", shlex.quote(recipe)]
    parts.append(shlex.quote(var))
    command = " ".join(parts)

    # kas writes its own INFO progress chatter to stderr. Split it into a
    # sibling capture so the stdout file holds nothing but the value.
    capture_path = log.run_dir / f"getvar-{var}.log"
    err_path = log.run_dir / f"getvar-{var}.err"
    rc = run_shell_capture(kas_ctx, command, capture_path, step="getvar", stderr_path=err_path)

    raw = capture_path.read_text(errors="replace") if capture_path.exists() else ""

    if rc != 0:
        console.print(f"[red]bitbake-getvar failed (exit {rc}).[/]")
        diagnostics = err_path.read_text(errors="replace") if err_path.exists() else ""
        if diagnostics.strip():
            console.print(diagnostics)
        if raw.strip():
            console.print(raw)
        raise typer.Exit(code=rc)

    # ``--value`` makes bitbake print the bare value and nothing else. Drop only
    # the single trailing newline it adds - a multi-line value (a shell function
    # body) may legitimately end in blank lines.
    value = raw.removesuffix("\n")

    if output_json:
        doc: dict = {"var": var, "value": value}
        if flag:
            doc["flag"] = flag
        if recipe:
            doc["recipe"] = recipe
        typer.echo(json.dumps(doc, indent=2))
    else:
        typer.echo(value)


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
        parts.append(shlex.quote(recipe))
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
        typer.echo("no history recorded")
    else:
        console.print(f"[bold]{var}[/] history (include-chain order):")
        for loc in locations:
            typer.echo(f"  {loc}")
