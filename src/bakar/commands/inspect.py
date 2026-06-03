"""bakar inspect subcommand - deep per-recipe report.

Combines three bitbake calls inside kas-container to produce a structured
report for a single recipe:

1. ``bitbake-layers show-recipes -f <recipe>`` - layer, recipe file, bbappends.
2. ``bitbake-getvar -r <recipe> WORKDIR S B D T`` - resolved build paths.
3. ``bitbake -e <recipe>`` - full environment dump parsed by
   :func:`~bakar.inspect_parse.parse_env_vars` for all other fields.

With ``--recursive/-r`` a fourth call adds transitive forward and reverse
dependency listings via ``bitbake-layers show-recipes`` and
``bitbake -g <recipe>``.

For an unknown recipe, the command exits non-zero and surfaces the bitbake
error rather than printing an empty report as success.
"""

from __future__ import annotations

import json
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
from bakar.inspect_parse import parse_env_vars
from bakar.observability import RunLogger
from bakar.steps.kas_build import KasBuildContext, run_shell_capture

# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _parse_show_recipes(text: str) -> dict[str, str]:
    """Parse ``bitbake-layers show-recipes <recipe>`` output (no -f flag).

    Without -f, show-recipes emits:

        === Available recipes: ===

        <pn>:
          <layer>                          <version>
          <layer2>                         <version2>

    Returns a dict with keys: ``layer`` (first layer listed), ``version``,
    ``recipe_file`` (always empty - read from env FILE variable instead),
    ``bbappends`` (always empty - not emitted by show-recipes).

    All values default to empty string when not found.
    """
    result: dict[str, str] = {"layer": "", "version": "", "recipe_file": "", "bbappends": ""}
    in_recipe_block = False

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("="):
            in_recipe_block = False
            continue
        # Recipe name header: "busybox:"
        if stripped.endswith(":") and not stripped.startswith(" ") and not stripped.startswith("\t"):
            in_recipe_block = True
            continue
        # Layer + version line: "  meta-oe                          1.36.1"
        # Lines inside a recipe block are indented
        if in_recipe_block and line.startswith((" ", "\t")) and stripped:
            parts = stripped.split()
            if parts and not result["layer"]:
                result["layer"] = parts[0]
                result["version"] = parts[1] if len(parts) > 1 else ""
            break  # first layer entry is the preferred provider

    return result


def _parse_getvar_paths(text: str) -> dict[str, str]:
    """Parse ``bitbake-getvar -r <recipe> WORKDIR S B D T`` output."""
    return parse_env_vars(text, ["WORKDIR", "S", "B", "D", "T"])


def _parse_inherits(env_text: str) -> list[str]:
    """Extract inherited bbclasses from ``bitbake -e`` output.

    Looks for the ``INHERITED`` variable which bitbake emits as a
    space-separated list of class names.
    """
    vars_ = parse_env_vars(env_text, ["INHERITED"])
    inherited = vars_.get("INHERITED", "")
    return [c.strip() for c in inherited.split() if c.strip()]


def _parse_packages_rdepends(env_text: str) -> list[dict[str, str | list[str]]]:
    """Extract PACKAGES and per-package RDEPENDS from ``bitbake -e`` output.

    Returns a list of dicts with keys ``package`` and ``rdepends`` (list[str]).
    """
    vars_ = parse_env_vars(env_text, ["PACKAGES"])
    packages_str = vars_.get("PACKAGES", "")
    packages = [p.strip() for p in packages_str.split() if p.strip()]

    # Per-package RDEPENDS are emitted as RDEPENDS_<pkg>="..." in older
    # bitbake and as RDEPENDS:<pkg>="..." in newer versions.
    rdepends_names = [f"RDEPENDS_{pkg}" for pkg in packages] + [f"RDEPENDS:{pkg}" for pkg in packages]
    rdepends_vars = parse_env_vars(env_text, rdepends_names)

    result: list[dict[str, str | list[str]]] = []
    for pkg in packages:
        rdeps_str = rdepends_vars.get(f"RDEPENDS_{pkg}", rdepends_vars.get(f"RDEPENDS:{pkg}", ""))
        rdeps = [d.strip() for d in rdeps_str.split() if d.strip()]
        result.append({"package": pkg, "rdepends": rdeps})
    return result


def _parse_recursive_deps(text: str) -> dict[str, list[str]]:
    """Parse ``bitbake -g <recipe>`` pn-buildlist output for transitive deps.

    ``bitbake -g`` writes ``pn-buildlist`` and ``task-depends.dot`` to the
    build dir. When we capture stdout, it contains the recipe list emitted
    to stdout. Returns a dict with ``forward`` and ``reverse`` lists.

    In practice we parse the captured output lines which contain recipe names.
    """
    forward: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and not stripped.startswith("NOTE"):
            forward.append(stripped)
    return {"forward": forward, "reverse": []}


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------


def _assemble_report(
    recipe: str,
    show_recipes_text: str,
    getvar_paths_text: str,
    env_text: str,
    recursive_text: str | None,
) -> dict:
    """Assemble the full inspect report dict from raw bitbake outputs."""
    # Identity
    show_info = _parse_show_recipes(show_recipes_text)
    identity_vars = parse_env_vars(env_text, ["PN", "PV", "PR", "FILE"])
    identity: dict[str, str] = {
        "PN": identity_vars.get("PN", recipe),
        "PV": identity_vars.get("PV", ""),
        "PR": identity_vars.get("PR", ""),
        "layer": show_info["layer"],
        "recipe_file": identity_vars.get("FILE", ""),
        "bbappends": show_info["bbappends"],
    }

    # Sources
    sources_vars = parse_env_vars(env_text, ["SRC_URI", "LICENSE", "LIC_FILES_CHKSUM"])
    sources: dict[str, str] = {
        "SRC_URI": sources_vars.get("SRC_URI", ""),
        "LICENSE": sources_vars.get("LICENSE", ""),
        "LIC_FILES_CHKSUM": sources_vars.get("LIC_FILES_CHKSUM", ""),
    }

    # Paths
    paths = _parse_getvar_paths(getvar_paths_text)

    # Inherits
    inherits = _parse_inherits(env_text)

    # Packages + per-package RDEPENDS
    packages = _parse_packages_rdepends(env_text)

    # Dependencies
    deps_vars = parse_env_vars(env_text, ["DEPENDS", "RDEPENDS"])
    dependencies: dict[str, object] = {
        "DEPENDS": [d.strip() for d in deps_vars.get("DEPENDS", "").split() if d.strip()],
        "RDEPENDS": [d.strip() for d in deps_vars.get("RDEPENDS", "").split() if d.strip()],
    }
    if recursive_text is not None:
        rec = _parse_recursive_deps(recursive_text)
        dependencies["transitive_forward"] = rec["forward"]
        dependencies["transitive_reverse"] = rec["reverse"]

    return {
        "identity": identity,
        "sources": sources,
        "paths": paths,
        "inherits": inherits,
        "packages": packages,
        "dependencies": dependencies,
    }


def _print_report(recipe: str, report: dict, *, output_json: bool) -> None:
    """Print the assembled report to stdout."""
    if output_json:
        typer.echo(json.dumps(report, indent=2))
        return

    identity = report["identity"]
    sources = report["sources"]
    paths = report.get("paths", {})
    inherits = report.get("inherits", [])
    packages = report.get("packages", [])
    dependencies = report.get("dependencies", {})

    # Identity
    console.print("[bold]Identity:[/]", highlight=False)
    console.print(f"  PN:          {identity.get('PN', '')}", highlight=False)
    console.print(f"  PV:          {identity.get('PV', '')}", highlight=False)
    console.print(f"  PR:          {identity.get('PR', '')}", highlight=False)
    console.print(f"  layer:       {identity.get('layer', '')}", highlight=False)
    console.print(f"  recipe_file: {identity.get('recipe_file', '')}", highlight=False)
    bbappends = identity.get("bbappends", "")
    if bbappends:
        console.print("  bbappends:", highlight=False)
        for a in bbappends.splitlines():
            console.print(f"    {a}", highlight=False)

    # Sources
    console.print("[bold]Sources:[/]", highlight=False)
    console.print(f"  LICENSE:          {sources.get('LICENSE', '')}", highlight=False)
    console.print(f"  LIC_FILES_CHKSUM: {sources.get('LIC_FILES_CHKSUM', '')}", highlight=False)
    src_uri = sources.get("SRC_URI", "")
    if src_uri:
        console.print("  SRC_URI:", highlight=False)
        for uri in src_uri.split():
            console.print(f"    {uri}", highlight=False)
    else:
        console.print("  SRC_URI: (none)", highlight=False)

    # Paths
    console.print("[bold]Paths:[/]", highlight=False)
    for var in ("WORKDIR", "S", "B", "D", "T"):
        val = paths.get(var, "")
        console.print(f"  {var}: {val}", highlight=False)

    # Inherits
    console.print("[bold]Inherits:[/]", highlight=False)
    if inherits:
        console.print(f"  {' '.join(inherits)}", highlight=False)
    else:
        console.print("  (none)", highlight=False)

    # Packages
    console.print("[bold]Packages:[/]", highlight=False)
    if packages:
        for pkg_info in packages:
            pkg = pkg_info["package"]
            rdeps = pkg_info["rdepends"]
            if rdeps:
                console.print(f"  {pkg}:", highlight=False)
                for rd in rdeps:  # type: ignore[union-attr]
                    console.print(f"    {rd}", highlight=False)
            else:
                console.print(f"  {pkg}", highlight=False)
    else:
        console.print("  (none)", highlight=False)

    # Dependencies
    console.print("[bold]Dependencies:[/]", highlight=False)
    depends = dependencies.get("DEPENDS", [])
    rdepends = dependencies.get("RDEPENDS", [])
    console.print("  build (DEPENDS):", highlight=False)
    if depends:
        for dep in depends:  # type: ignore[union-attr]
            console.print(f"    {dep}", highlight=False)
    else:
        console.print("    (none)", highlight=False)
    console.print("  runtime (RDEPENDS):", highlight=False)
    if rdepends:
        for dep in rdepends:  # type: ignore[union-attr]
            console.print(f"    {dep}", highlight=False)
    else:
        console.print("    (none)", highlight=False)

    if "transitive_forward" in dependencies:
        console.print("  transitive forward deps:", highlight=False)
        for dep in dependencies["transitive_forward"]:  # type: ignore[union-attr]
            console.print(f"    {dep}", highlight=False)
    if "transitive_reverse" in dependencies:
        console.print("  transitive reverse deps:", highlight=False)
        for dep in dependencies["transitive_reverse"]:  # type: ignore[union-attr]
            console.print(f"    {dep}", highlight=False)


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------


@app.command("inspect")
def inspect(
    recipe: Annotated[
        str,
        typer.Argument(help="Recipe name to inspect (e.g. busybox, core-image-minimal)."),
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
    output_json: Annotated[
        bool,
        typer.Option("--json", help="Emit the report as a JSON document"),
    ] = False,
    recursive: Annotated[
        bool,
        typer.Option("--recursive", "-r", help="Include transitive forward and reverse dependencies"),
    ] = False,
) -> None:
    """Print a deep per-recipe inspection report.

    Combines three bitbake calls inside kas-container:

    \b
    1. bitbake-layers show-recipes -f <recipe>  (Identity: layer, recipe file, bbappends)
    2. bitbake-getvar -r <recipe> WORKDIR S B D T  (Paths)
    3. bitbake -e <recipe>  (Sources, Inherits, Packages, Dependencies)

    With ``--recursive/-r`` a fourth call (``bitbake -g <recipe>``) adds
    transitive forward and reverse dependency listings.

    Exits non-zero when the recipe is unknown, surfacing the bitbake error
    rather than printing an empty report.
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

        # --- Step 1: layer, recipe file, bbappends ---
        show_recipes_out = log.run_dir / "inspect-show-recipes.log"
        rc_show = run_shell_capture(
            kas_ctx,
            f"bitbake-layers show-recipes {shlex.quote(recipe)}",
            show_recipes_out,
            step="inspect_show_recipes",
        )
        show_recipes_text = show_recipes_out.read_text(errors="replace") if show_recipes_out.exists() else ""

        if rc_show != 0:
            console.print(
                f"[red]bitbake-layers show-recipes failed for recipe '{recipe}' (exit {rc_show}).[/]\n"
                f"{show_recipes_text}"
            )
            raise typer.Exit(code=rc_show)

        # --- Step 2: resolved build paths ---
        getvar_paths_out = log.run_dir / "inspect-getvar-paths.log"
        rc_paths = run_shell_capture(
            kas_ctx,
            f"bitbake-getvar -r {shlex.quote(recipe)} WORKDIR S B D T",
            getvar_paths_out,
            step="inspect_getvar_paths",
        )
        getvar_paths_text = getvar_paths_out.read_text(errors="replace") if getvar_paths_out.exists() else ""

        if rc_paths != 0:
            console.print(
                f"[red]bitbake-getvar failed for recipe '{recipe}' (exit {rc_paths}).[/]\n{getvar_paths_text}"
            )
            raise typer.Exit(code=rc_paths)

        # --- Step 3: full environment dump ---
        env_out = log.run_dir / "inspect-env.log"
        rc_env = run_shell_capture(
            kas_ctx,
            f"bitbake -e {shlex.quote(recipe)}",
            env_out,
            step="inspect_env",
        )
        env_text = env_out.read_text(errors="replace") if env_out.exists() else ""

        if rc_env != 0:
            console.print(f"[red]bitbake -e {recipe} failed (exit {rc_env}).[/]\n{env_text}")
            raise typer.Exit(code=rc_env)

        # --- Step 4 (optional): transitive deps ---
        recursive_text: str | None = None
        if recursive:
            recursive_out = log.run_dir / "inspect-recursive.log"
            rc_rec = run_shell_capture(
                kas_ctx,
                f"bitbake -g {shlex.quote(recipe)}",
                recursive_out,
                step="inspect_recursive",
            )
            recursive_text = recursive_out.read_text(errors="replace") if recursive_out.exists() else ""
            if rc_rec != 0:
                console.print(f"[red]bitbake -g {recipe} failed (exit {rc_rec}).[/]\n{recursive_text}")
                raise typer.Exit(code=rc_rec)

    report = _assemble_report(recipe, show_recipes_text, getvar_paths_text, env_text, recursive_text)
    _print_report(recipe, report, output_json=output_json)
