"""bakar diffsigs subcommand - why did this task rebuild?

Runs ``bitbake -S printdiff <recipe>`` inside kas-container to generate
sigdata, then ``bitbake-diffsigs -t <recipe> <task>`` to render the
per-variable old-vs-new differences. Requires a prior build so the
reference sigdata files exist.
"""

from __future__ import annotations

import ast
import re
import shlex
from pathlib import Path
from typing import Annotated

import typer

import bakar.commands._app as _state
from bakar.commands._app import app, console
from bakar.commands._helpers import (
    _normalize_dispatch,
    _overlay_for,
    _resolve_workspace,
)
from bakar.config import BSPSpec, resolve
from bakar.observability import RunLogger
from bakar.steps.kas_build import KasBuildContext, run_shell_capture

# ---------------------------------------------------------------------------
# Output parsing helpers
# ---------------------------------------------------------------------------

_KAS_LOG_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} - (?:INFO|WARNING|ERROR)\s+-")
_CHAIN_RE = re.compile(r"^(\s*)Hash for task dependency (\S+) changed")
_CAUSE_RE = re.compile(r"Dependency on (variable|function) (\S+) was (added|removed|changed)")
_BASEHASH_RE = re.compile(r"basehash changed from")


def _strip_kas_preamble(lines: list[str]) -> list[str]:
    """Drop kas-container startup log lines (timestamp + level prefix)."""
    return [line for line in lines if not _KAS_LOG_RE.match(line)]


def _extract_dep_diff(lines: list[str]) -> tuple[list[str], list[str]]:
    """Parse 'Task dependencies changed from: [...] to: [...]' and return (added, removed)."""
    text = "\n".join(lines)
    from_match = re.search(r"Task dependencies changed from:\s*(\[.*?\])\s*to:\s*(\[.*?\])", text, re.DOTALL)
    if not from_match:
        return [], []
    try:
        from_list: list[str] = ast.literal_eval(from_match.group(1))
        to_list: list[str] = ast.literal_eval(from_match.group(2))
    except ValueError, SyntaxError:
        return [], []
    from_set, to_set = set(from_list), set(to_list)
    added = sorted(to_set - from_set)
    removed = sorted(from_set - to_set)
    return added, removed


def _recipe_from_task(task: str) -> str:
    """Return the recipe portion of a 'recipe:do_task' string."""
    return task.split(":")[0] if ":" in task else task


def _render_diffsigs(text: str) -> None:
    """Parse and render bitbake-diffsigs output: root cause, chain, dep diff."""
    lines = text.splitlines()
    clean = _strip_kas_preamble(lines)

    # -- Causal chain (ordered by indentation depth, shallowest first) --
    chain: list[tuple[int, str]] = []
    for line in clean:
        m = _CHAIN_RE.match(line)
        if m:
            chain.append((len(m.group(1)), m.group(2)))
    chain.sort(key=lambda x: x[0])

    # -- Root cause: all "Dependency on variable/function X was ..." lines --
    causes: list[str] = []
    for line in clean:
        m = _CAUSE_RE.search(line)
        if m:
            causes.append(line.strip())

    # -- basehash change present? --
    basehash_changed = any(_BASEHASH_RE.search(line) for line in clean)

    # -- Dep-list diff --
    added, removed = _extract_dep_diff(clean)

    # -- Render --

    # 1. Root cause with count when multiple
    if causes:
        label = "Root cause" if len(causes) == 1 else f"Root causes ({len(causes)})"
        console.print(f"[bold yellow]{label}:[/]", highlight=False)
        for c in causes:
            console.print(f"  {c}", highlight=False)
        if basehash_changed and (added or removed):
            console.print(
                "  [dim](basehash changed because the dep list changed above)[/dim]",
                highlight=False,
            )
        elif basehash_changed:
            console.print(
                "  [dim](basehash changed independently — task function or referenced code changed)[/dim]",
                highlight=False,
            )
        console.print()

    # 2. Chain with depth and cross-recipe boundary note
    if chain:
        depth = len(chain)
        depth_note = f"{depth} level{'s' if depth != 1 else ''} deep"
        requested_recipe = _recipe_from_task(chain[0][1])
        root_recipe = _recipe_from_task(chain[-1][1])
        cross_note = ""
        if depth > 1 and root_recipe != requested_recipe:
            cross_note = f"  [dim]cross-recipe: {root_recipe} → {requested_recipe}[/dim]"

        console.print(f"[bold]Rebuild chain[/]  ({depth_note}){cross_note}:", highlight=False)
        for i, (_, task_name) in enumerate(chain):
            if i == 0:
                console.print(f"  {task_name}  [dim]← requested[/dim]", highlight=False)
            elif i == depth - 1:
                console.print(f"  {'  ' * i}↳ {task_name}  [bold yellow]← root cause[/bold yellow]", highlight=False)
            else:
                console.print(f"  {'  ' * i}↳ {task_name}", highlight=False)
        console.print()

    # 3. Dep-list diff with count summary
    if added or removed:
        parts = []
        if added:
            parts.append(f"[green]{len(added)} added[/green]")
        if removed:
            parts.append(f"[red]{len(removed)} removed[/red]")
        summary = ", ".join(parts)
        console.print(f"[bold]Dependency list diff[/]  ({summary}):", highlight=False)
        for a in added:
            console.print(f"  [green]+ {a}[/green]", highlight=False)
        for r in removed:
            console.print(f"  [red]- {r}[/red]", highlight=False)
        console.print()

    if not causes and not chain:
        # Fallback: no structure found, print the kas-stripped text
        console.print("\n".join(clean), highlight=False)


@app.command("diffsigs")
def diffsigs(
    recipe: Annotated[
        str,
        typer.Argument(help="Recipe name to inspect (e.g. busybox, core-image-minimal)."),
    ],
    task: Annotated[
        str,
        typer.Argument(help="Task name to inspect (e.g. do_compile, do_fetch)."),
    ],
    kas_yaml: Annotated[
        Path | None,
        typer.Argument(
            exists=False,
            help="Optional kas YAML (BYO/bbsetup); resolves the workspace next to it.",
        ),
    ] = None,
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
    raw: Annotated[
        bool,
        typer.Option("--raw", help="Print the full unprocessed bitbake-diffsigs output including kas startup lines"),
    ] = False,
) -> None:
    """Show why a task missed sstate and rebuilt.

    Runs ``bitbake -S printdiff <recipe>`` to generate sigdata, then
    ``bitbake-diffsigs -t <recipe> <task>`` to render per-variable
    old-vs-new differences. Requires a prior build so the reference
    sigdata files exist.

    When no prior sigdata is found, exits non-zero with a clear message
    rather than printing an empty diff.
    """
    family, bsp, kas_yaml, manifest = _normalize_dispatch(kas_yaml, manifest)
    ws = _resolve_workspace(workspace, kas_yaml=kas_yaml, family=family)
    cfg = resolve(
        workspace=ws,
        bsp_family=family,
        spec=BSPSpec(manifest=manifest, machine=machine),
        kas_yaml=kas_yaml,
        user_config=_state._USER_CONFIG,
    )
    overlay_source = _overlay_for(bsp)
    cfg.runs_dir.mkdir(parents=True, exist_ok=True)

    with RunLogger(runs_dir=cfg.runs_dir) as log:
        kas_ctx = KasBuildContext(cfg, log, cfg.kas_yaml, overlay_source)

        # Step 1: generate sigdata by running bitbake with the printdiff signature handler.
        printdiff_out = log.run_dir / "diffsigs-printdiff.log"
        rc_printdiff = run_shell_capture(
            kas_ctx,
            f"bitbake -S printdiff {shlex.quote(recipe)}",
            printdiff_out,
            step="diffsigs_printdiff",
        )
        if rc_printdiff != 0:
            console.print(
                f"[red]bitbake -S printdiff {recipe} failed (exit {rc_printdiff}).[/]\n"
                "Check that the workspace is synced and the recipe name is correct."
            )
            raise typer.Exit(code=rc_printdiff)

        # Step 2: render the per-variable differences between sigdata files.
        diffsigs_out = log.run_dir / "diffsigs-render.log"
        rc_diffsigs = run_shell_capture(
            kas_ctx,
            f"bitbake-diffsigs -t {shlex.quote(recipe)} {shlex.quote(task)}",
            diffsigs_out,
            step="diffsigs_render",
        )

        if rc_diffsigs != 0:
            # Distinguish missing-sigdata from other errors by inspecting output.
            err_text = diffsigs_out.read_text(errors="replace") if diffsigs_out.exists() else ""
            missing_sigdata = "No such file" in err_text or not err_text.strip()
            if missing_sigdata:
                console.print(
                    f"[red]Required sigdata for {recipe}:{task} does not exist.[/]\n"
                    "Run a build first so bitbake writes the reference sigdata stamps,\n"
                    "then re-run: bakar diffsigs"
                )
            else:
                console.print(f"[red]bitbake-diffsigs failed (exit {rc_diffsigs}).[/]\n{err_text}")
            raise typer.Exit(code=rc_diffsigs)

        diff_text = diffsigs_out.read_text(errors="replace") if diffsigs_out.exists() else ""
        console.print(f"[bold]diffsigs:[/] {recipe} {task}\n")
        if raw:
            console.print(diff_text, highlight=False)
        else:
            _render_diffsigs(diff_text)
