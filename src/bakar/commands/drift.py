"""bakar drift subcommand - compare workspace sources against pinned state.

For each cloned source repo under the workspace, reports the pinned SHA (from
the manifest XML for NXP/TI families, or the kas lockfile for BYO/bbsetup)
against the actual on-disk HEAD. Sources whose HEAD matches the pin are clean;
``--all`` includes them in the output.

Pin-key reconciliation: manifest pins use keys like ``"sources/meta-imx"``
(relative to ``bsp_root``), while :func:`~bakar.layers.discover_source_repos`
returns bare names like ``"meta-imx"``. This module normalises by stripping any
leading path component (``"sources/"``, ``"layers/"`` etc.) when building the
name-to-pin map for NXP/TI families.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

import bakar.commands._app as _state
from bakar import pin_state
from bakar.bsp_detect import is_meta_avocado_yaml, meta_avocado_checkout
from bakar.commands._app import app, console
from bakar.commands._helpers import (
    WorkspaceOption,
    _normalize_dispatch,
    _resolve_workspace,
    global_container_mode,
    global_host_mode,
)
from bakar.commands._sync_pins import printable
from bakar.config import BSPSpec, ResolveRequest, resolve
from bakar.layers import discover_source_repos
from bakar.release_workspace import ReleaseRoutingError
from bakar.steps import kas_pins, repo_reuse

# Reuse the authoritative constant from pin_state so the two modules stay in sync.
_MANIFEST_FAMILIES = pin_state._MANIFEST_FAMILIES


def _locate_lockfile(cfg_bsp_root: Path, kas_yaml: Path | None) -> Path | None:
    """Return the kas lockfile path when it exists, or None.

    Looks for ``kas.lock`` next to the kas YAML (BYO convention) or at
    ``<bsp_root>/kas.lock`` (bbsetup convention).
    """
    candidates: list[Path] = []
    if kas_yaml is not None:
        candidates.append(kas_yaml.parent / "kas.lock")
    candidates.append(cfg_bsp_root / "kas.lock")
    for cand in candidates:
        if cand.is_file():
            return cand
    return None


@app.command("drift")
def drift(
    kas_yaml: Annotated[
        Path | None,
        typer.Argument(help="kas YAML (BYO/bbsetup). Omit when using --manifest/-f for NXP/TI."),
    ] = None,
    manifest: Annotated[
        str | None,
        typer.Option("--manifest", "-f", help="Manifest filename (NXP/TI) or kas YAML path."),
    ] = None,
    workspace: WorkspaceOption = None,
    show_json: Annotated[
        bool,
        typer.Option("--json", help="Emit JSON array to stdout instead of plain text."),
    ] = False,
    fmt: Annotated[
        str,
        typer.Option("--format", help="Output format: text (default) or md (markdown table)."),
    ] = "text",
    show_all: Annotated[
        bool,
        typer.Option("--all", help="Include clean (non-drifted) sources in output."),
    ] = False,
) -> None:
    """Compare each cloned source repo's HEAD against its pinned revision.

    Pin sources differ by workspace family:

    - **NXP/TI**: pins are read from the manifest XML (``--manifest/-f``).
    - **BYO/bbsetup**: pins are read from a ``kas.lock`` file next to the kas
      YAML, falling back to each source's current git HEAD (zero drift reported).

    Exits 0 when no sources have drifted (or when ``--all`` is used).
    Exits 2 when the pin input required by the family is not found.
    """
    if fmt not in ("text", "md"):
        console.print(f"[red]Unknown format:[/] {fmt!r}. Valid values: text, md")
        raise typer.Exit(code=2)

    family, _bsp, kas_yaml, manifest = _normalize_dispatch(kas_yaml, manifest)
    ws = _resolve_workspace(workspace, kas_yaml=kas_yaml, family=family)
    cfg = resolve(
        ResolveRequest(
            workspace=ws,
            bsp_family=family,
            # The global --host/--container flags decide host_mode as they do for a build.
            spec=BSPSpec(manifest=manifest, host_mode=global_host_mode(), container_mode=global_container_mode()),
            kas_yaml=kas_yaml,
            user_config=_state._USER_CONFIG,
        )
    )

    # -- meta-avocado: pins come from the kas file's merged configuration ----
    if kas_yaml is not None and family not in _MANIFEST_FAMILIES and is_meta_avocado_yaml(kas_yaml):
        checkout = meta_avocado_checkout(kas_yaml)
        if checkout is not None:
            rows = _meta_avocado_rows(kas_yaml, checkout, ws, show_all, host_mode=cfg.host_mode)
            _emit(rows, show_json, fmt, show_all, meta_avocado=True)

    # -- Resolve pin source -----------------------------------------------
    raw_pins: dict[str, str]
    if family in _MANIFEST_FAMILIES:
        if not cfg.manifest_path.is_file():
            console.print(
                f"[red]Manifest not found:[/] {cfg.manifest_path}\n"
                f"Run 'bakar sync' first or pass --manifest to name the manifest."
            )
            raise typer.Exit(code=2)
        try:
            raw_pins = pin_state.read_pins(family, manifest=cfg.manifest_path)
        except ValueError as exc:
            console.print(f"[red]Cannot read pins:[/] {exc}")
            raise typer.Exit(code=2) from None
    else:
        lockfile = _locate_lockfile(cfg.bsp_root, kas_yaml)
        try:
            raw_pins = pin_state.read_pins(
                family,
                lockfile=lockfile,
                workspace=cfg.bsp_root,
            )
        except ValueError as exc:
            console.print(f"[red]Cannot read pins:[/] {exc}")
            raise typer.Exit(code=2) from None

    pin_lookup = pin_state._normalize_pin_keys(raw_pins, is_manifest=family in _MANIFEST_FAMILIES)

    # -- Enumerate sources ------------------------------------------------
    sources = discover_source_repos(cfg)

    rows: list[dict[str, object]] = []
    for name, path in sources:
        pinned = pin_lookup.get(name)
        actual = pin_state._git_head(path)
        if actual is None:
            continue  # unreadable checkout - skip silently

        if pinned is None:
            # Source not in pin map (e.g., untracked clone). Only show with --all.
            if show_all:
                rows.append(
                    {
                        "source": name,
                        "pinned": None,
                        "actual": actual,
                        "distance": None,
                        "drifted": False,
                    }
                )
            continue

        drifted = pinned != actual
        distance: int | None = None
        if drifted:
            distance = pin_state.commit_distance(path, pinned, actual)

        if drifted or show_all:
            rows.append(
                {
                    "source": name,
                    "pinned": pinned,
                    "actual": actual,
                    "distance": distance,
                    "drifted": drifted,
                }
            )

    _emit(rows, show_json, fmt, show_all, meta_avocado=False)


def _meta_avocado_rows(
    kas_yaml: Path, checkout: Path, ws: Path, show_all: bool, *, host_mode: bool
) -> list[dict[str, object]]:
    """Build drift rows from the merged kas configuration of a meta-avocado kas file.

    ``host_mode`` is the build's mode: only a host-mode build pairs a diverged
    openembedded-core with a frozen bitbake, so a container build refuses it.
    """
    try:
        merged = kas_pins.read_merged_config(kas_yaml, checkout=checkout)
    except ReleaseRoutingError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(code=2) from None
    entries = repo_reuse.pinned_entries(merged.get("repos") or {}, ws)
    facts = repo_reuse.gather_local_facts(entries)
    # A clone the build refuses (dirty bitbake, a missing or malformed pin) drifts even at its pin.
    states = repo_reuse.classify_facts(entries, facts, pair_core_with_bitbake=host_mode)
    rows: list[dict[str, object]] = []
    for entry, fact, state in zip(entries, facts, states, strict=True):
        missing = not fact.exists
        pinned = entry.pin
        actual = fact.head if fact.exists else None
        distance: int | None = None
        if pinned is not None and fact.pin_present and actual is not None:
            distance = pin_state.commit_distance(entry.path, pinned, actual)
        at_pin = pinned is not None and (actual or "").lower().startswith(pinned.lower())
        drifted = missing or (pinned is not None and not at_pin) or state.blocking is not None
        if drifted and distance == 0 and not at_pin and pinned is not None and actual is not None:
            # pinned..actual is empty when the clone sits behind its pin: count the other way, negative.
            behind = pin_state.commit_distance(entry.path, actual, pinned)
            distance = -behind if behind else None
        if drifted or show_all:
            rows.append(
                {
                    "source": entry.name,
                    "pinned": pinned,
                    "actual": actual,
                    "distance": distance,
                    "drifted": drifted,
                    "missing": missing,
                }
            )
    return rows


def _emit(rows: list[dict[str, object]], show_json: bool, fmt: str, show_all: bool, *, meta_avocado: bool) -> None:
    """Render ``rows`` and exit 1 when any drifted, else 0."""
    if show_json:
        keys = ("source", "pinned", "actual", "distance")
        if meta_avocado:
            keys += ("drifted", "missing")
        out = [{k: r[k] for k in keys} for r in rows]
        typer.echo(json.dumps(out, indent=2))
        raise typer.Exit(code=1 if any(r["drifted"] for r in rows) else 0)

    if not rows:
        if show_all:
            console.print("No sources found.")
        else:
            console.print("All sources are on their pinned revision.")
        raise typer.Exit(code=0)

    if fmt == "md":
        _render_markdown(rows)
    else:
        _render_text(rows)

    has_drift = any(r["drifted"] for r in rows)
    raise typer.Exit(code=1 if has_drift else 0)


def _columns(r: dict[str, object]) -> tuple[str, str, str, str, str]:
    """``(source, pinned, actual, distance, status)`` for one row, safe to print.

    Names and pins come from kas files, so control characters are replaced before
    they reach the terminal; the caller prints with markup off.
    """
    pinned_col = printable(str(r["pinned"])[:8]) if r["pinned"] else "-"
    actual_col = "missing" if r.get("missing") else (printable(str(r["actual"])[:8]) if r["actual"] else "-")
    dist_col = f"{r['distance']:+d}" if r["distance"] is not None else ("?" if r["drifted"] else "")
    status = "DRIFTED" if r["drifted"] else "clean"
    return printable(str(r["source"])), pinned_col, actual_col, dist_col, status


def _render_text(rows: list[dict[str, object]]) -> None:
    """Print a plain-text table to the console."""
    console.print(f"{'Source':<30}  {'Pinned':>10}  {'Actual':>10}  {'Distance':>8}  Status")
    console.print("-" * 75)
    for r in rows:
        source, pinned_col, actual_col, dist_col, status = _columns(r)
        console.print(
            f"{source:<30}  {pinned_col:>10}  {actual_col:>10}  {dist_col:>8}  {status}", markup=False, highlight=False
        )


def _render_markdown(rows: list[dict[str, object]]) -> None:
    """Print a markdown table to the console."""
    console.print("| Source | Pinned | Actual | Distance | Status |")
    console.print("|--------|--------|--------|----------|--------|")
    for r in rows:
        source, pinned_col, actual_col, dist_col, status = _columns(r)
        console.print(
            f"| {source} | {pinned_col} | {actual_col} | {dist_col} | {status} |", markup=False, highlight=False
        )
