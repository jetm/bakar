"""bakar graph subcommand - recipe dependency-graph analysis.

Runs ``bitbake -g <recipe>`` inside kas-container and analyzes the emitted
``task-depends.dot`` and ``pn-buildlist`` artifacts with the pure
:mod:`bakar.graph_analyze` module.

The artifacts land in ``${TOPDIR}`` inside the container, so the command first
resolves ``${TOPDIR}`` via ``bitbake-getvar -r <recipe> TOPDIR`` (the same path
as ``bakar getvar``), then ``cat``s the two files. Resolving ``TOPDIR`` rather
than reading a fixed host subpath keeps retrieval family-agnostic: the bbsetup
family's ``bsp_root`` is the workspace root, so no fixed in-container build dir
maps back to the host across families.

Output is selectable with ``--format``:

- ``text`` (default): a human-readable report (package count, blast radius,
  longest chain, cycle report, critical recipes).
- ``dot``: the raw ``task-depends.dot``.
- ``json``: a machine-readable document carrying every insight, including
  ``blast_radius``.

When a buildhistory ``depends.dot`` exists under
``cfg.bsp_root/build/buildhistory/``, a top-runtime-packages-by-fan-in section
is appended; it is omitted without error when absent.

For an unknown recipe or a failing ``bitbake -g``, the command exits non-zero
and surfaces the bitbake error rather than printing empty graph data as
success.
"""

from __future__ import annotations

import json
import shlex
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer

import bakar.commands._app as _state
from bakar.commands._app import app, console
from bakar.commands._helpers import (
    WorkspaceOption,
    _normalize_dispatch,
    _overlay_for,
    _resolve_workspace,
)
from bakar.config import BSPSpec, resolve
from bakar.graph_analyze import analyze, top_runtime_packages
from bakar.inspect_parse import parse_getvar_value
from bakar.observability import RunLogger
from bakar.steps.kas_build import KasBuildContext, run_shell_capture


class GraphFormat(StrEnum):
    """Output format for ``bakar graph``."""

    text = "text"
    dot = "dot"
    json = "json"


def _find_buildhistory_depends_dot(bsp_root: Path) -> Path | None:
    """Locate a buildhistory ``depends.dot`` under ``bsp_root/build/buildhistory``.

    Returns the first matching path, or ``None`` when the buildhistory tree is
    absent. Buildhistory emits its package-level runtime-dependency graph as
    ``depends.dot`` somewhere beneath ``buildhistory/``; a recursive search keeps
    the lookup robust to the exact subdir layout.
    """
    bh_root = bsp_root / "build" / "buildhistory"
    if not bh_root.is_dir():
        return None
    matches = sorted(bh_root.rglob("depends.dot"))
    return matches[0] if matches else None


def _print_text_report(recipe: str, insights: dict, runtime: list[tuple[str, int]]) -> None:
    """Render the human-readable text report."""
    console.print(f"[bold]Dependency graph for {recipe}[/]", highlight=False)

    pkg_count = insights["package_count"]
    console.print(f"  packages in scope: {pkg_count}", highlight=False)

    direct = insights.get("direct_deps", [])
    console.print(f"  direct deps: {len(direct)}", highlight=False)

    depth = insights.get("depth")
    radius_label = "transitive deps" if depth is None else f"transitive deps (depth {depth})"
    console.print(f"  {radius_label}: {insights['blast_radius']}", highlight=False)

    chain = insights.get("longest_chain", [])
    console.print("[bold]Longest build chain:[/]", highlight=False)
    if chain:
        console.print(f"  length {len(chain)}: {' -> '.join(chain)}", highlight=False)
    else:
        console.print("  (none)", highlight=False)

    cycle = insights.get("cycle", [])
    console.print("[bold]Cycle report:[/]", highlight=False)
    if cycle:
        console.print(f"  cycle: {' -> '.join(cycle)} -> ...", highlight=False)
    else:
        console.print("  no cycles", highlight=False)

    critical = insights.get("critical", [])
    console.print("[bold]Most depended-on recipes:[/]", highlight=False)
    if critical:
        for name, degree in critical:
            console.print(f"  {name} ({degree} dependents)", highlight=False)
    else:
        console.print("  (none)", highlight=False)

    if runtime:
        console.print("[bold]Top runtime packages (buildhistory fan-in):[/]", highlight=False)
        for name, degree in runtime:
            console.print(f"  {name} ({degree})", highlight=False)


@app.command("graph")
def graph(
    recipe: Annotated[
        str,
        typer.Argument(help="Recipe name to analyze (e.g. busybox, core-image-minimal)."),
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
    workspace: WorkspaceOption = None,
    output_format: Annotated[
        GraphFormat,
        typer.Option("--format", help="Output format: text (default), dot, or json."),
    ] = GraphFormat.text,
    depth: Annotated[
        int | None,
        typer.Option("--depth", help="Bound transitive dependency expansion to N levels."),
    ] = None,
) -> None:
    """Analyze a recipe's BitBake dependency graph.

    Runs ``bitbake -g <recipe>`` inside kas-container, retrieves the emitted
    ``task-depends.dot`` and ``pn-buildlist`` from ``${TOPDIR}``, and reports
    package count, blast radius, longest build chain, cycles, and critical
    recipes. ``--format dot`` prints the raw dot; ``--format json`` emits a
    machine-readable document including ``blast_radius``.

    Exits non-zero when ``bitbake -g`` fails, surfacing the bitbake error
    rather than printing empty graph data as success.
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

        # --- Step 1: resolve ${TOPDIR} for this recipe ---
        topdir_out = log.run_dir / "graph-topdir.log"
        rc_topdir = run_shell_capture(
            kas_ctx,
            f"bitbake-getvar -r {shlex.quote(recipe)} TOPDIR",
            topdir_out,
            step="graph_topdir",
        )
        topdir_text = topdir_out.read_text(errors="replace") if topdir_out.exists() else ""

        if rc_topdir != 0:
            console.print(f"[red]bitbake-getvar TOPDIR failed for recipe '{recipe}' (exit {rc_topdir}).[/]")
            if topdir_text.strip():
                console.print(topdir_text)
            raise typer.Exit(code=rc_topdir)

        topdir = parse_getvar_value(topdir_text, "TOPDIR").strip()
        if not topdir or "\n" in topdir:
            console.print(f"[red]could not resolve TOPDIR for recipe '{recipe}'.[/]")
            if topdir_text.strip():
                console.print(topdir_text)
            raise typer.Exit(code=1)

        # --- Step 2: generate the dependency graph ---
        graph_out = log.run_dir / "graph-bitbake-g.log"
        rc_graph = run_shell_capture(
            kas_ctx,
            f"bitbake -g {shlex.quote(recipe)}",
            graph_out,
            step="graph_generate",
        )
        graph_text = graph_out.read_text(errors="replace") if graph_out.exists() else ""

        if rc_graph != 0:
            console.print(f"[red]bitbake -g {recipe} failed (exit {rc_graph}).[/]")
            if graph_text.strip():
                console.print(graph_text)
            raise typer.Exit(code=rc_graph)

        # --- Step 3: retrieve the two artifacts from ${TOPDIR} ---
        dot_out = log.run_dir / "graph-task-depends.dot"
        rc_dot = run_shell_capture(
            kas_ctx,
            f"cat {shlex.quote(topdir)}/task-depends.dot",
            dot_out,
            step="graph_task_depends",
        )
        dot_text = dot_out.read_text(errors="replace") if dot_out.exists() else ""

        if rc_dot != 0:
            console.print(f"[red]could not read {topdir}/task-depends.dot (exit {rc_dot}).[/]")
            if dot_text.strip():
                console.print(dot_text)
            raise typer.Exit(code=rc_dot)

        # --format dot needs only the raw graph; skip the pn-buildlist
        # retrieval, analysis, and buildhistory read the other formats consume.
        if output_format is GraphFormat.dot:
            typer.echo(dot_text)
            return

        buildlist_out = log.run_dir / "graph-pn-buildlist.log"
        rc_buildlist = run_shell_capture(
            kas_ctx,
            f"cat {shlex.quote(topdir)}/pn-buildlist",
            buildlist_out,
            step="graph_pn_buildlist",
        )
        buildlist_text = buildlist_out.read_text(errors="replace") if buildlist_out.exists() else ""

        if rc_buildlist != 0:
            console.print(f"[red]could not read {topdir}/pn-buildlist (exit {rc_buildlist}).[/]")
            if buildlist_text.strip():
                console.print(buildlist_text)
            raise typer.Exit(code=rc_buildlist)

    insights = analyze(dot_text, buildlist_text, recipe, depth=depth)

    # Optional buildhistory runtime-dependency section (host-side read).
    runtime: list[tuple[str, int]] = []
    bh_dot = _find_buildhistory_depends_dot(cfg.bsp_root)
    if bh_dot is not None:
        runtime = top_runtime_packages(bh_dot.read_text(errors="replace"))

    if output_format is GraphFormat.json:
        doc = dict(insights)
        if runtime:
            doc["top_runtime_packages"] = runtime
        typer.echo(json.dumps(doc, indent=2))
        return

    _print_text_report(recipe, insights, runtime)
