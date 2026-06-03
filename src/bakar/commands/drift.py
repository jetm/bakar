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
from bakar.commands._app import app, console
from bakar.commands._helpers import _normalize_dispatch, _resolve_workspace
from bakar.config import BSPSpec, resolve
from bakar.layers import discover_source_repos

# Reuse the authoritative constant from pin_state so the two modules stay in sync.
_MANIFEST_FAMILIES = pin_state._MANIFEST_FAMILIES


def _strip_path_prefix(pin_key: str) -> str:
    """Return the bare repo name from a manifest pin key.

    Manifest pins use keys like ``"sources/meta-imx"``; stripping the first
    path component gives the bare name that matches the directory entry under
    ``sources/`` or ``layers/``.
    """
    parts = pin_key.split("/", 1)
    return parts[-1]


def _build_pin_lookup(pins: dict[str, str], family: str) -> dict[str, str]:
    """Return ``{bare_name: pinned_sha}`` from a raw pins dict.

    For NXP/TI the raw keys contain a leading path component
    (``"sources/meta-imx"``); for BYO/bbsetup the keys are already bare names
    (``"meta-avocado"``). This normalises both shapes to bare names so they can
    be matched against :func:`~bakar.layers.discover_source_repos` output.
    """
    if family in _MANIFEST_FAMILIES:
        return {_strip_path_prefix(k): v for k, v in pins.items()}
    return dict(pins)


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
    workspace: Annotated[
        Path | None,
        typer.Option("--workspace", "-w", help="Workspace root override."),
    ] = None,
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
    family, _bsp, kas_yaml, manifest = _normalize_dispatch(kas_yaml, manifest)
    ws = _resolve_workspace(workspace, kas_yaml=kas_yaml, family=family)
    cfg = resolve(
        workspace=ws,
        bsp_family=family,
        spec=BSPSpec(manifest=manifest),
        kas_yaml=kas_yaml,
        user_config=_state._USER_CONFIG,
    )

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

    pin_lookup = _build_pin_lookup(raw_pins, family)

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

    # -- Render output ----------------------------------------------------
    if show_json:
        out = [{k: r[k] for k in ("source", "pinned", "actual", "distance")} for r in rows]
        console.print(json.dumps(out))
        raise typer.Exit(code=1 if any(r["drifted"] for r in rows) else 0)

    if not rows:
        if show_all:
            console.print("No sources found.")
        else:
            console.print("All sources are on their pinned revision.")
        raise typer.Exit(code=0)

    if fmt not in ("text", "md"):
        console.print(f"[red]Unknown format:[/] {fmt!r}. Valid values: text, md")
        raise typer.Exit(code=2)

    if fmt == "md":
        _render_markdown(rows)
    else:
        _render_text(rows)

    has_drift = any(r["drifted"] for r in rows)
    raise typer.Exit(code=1 if has_drift else 0)


def _render_text(rows: list[dict[str, object]]) -> None:
    """Print a plain-text table to the console."""
    console.print(f"{'Source':<30}  {'Pinned':>10}  {'Actual':>10}  {'Distance':>8}  Status")
    console.print("-" * 75)
    for r in rows:
        pinned_col = str(r["pinned"])[:8] if r["pinned"] else "-"
        actual_col = str(r["actual"])[:8] if r["actual"] else "-"
        dist_col = f"+{r['distance']}" if r["distance"] is not None else ("?" if r["drifted"] else "")
        status = "DRIFTED" if r["drifted"] else "clean"
        console.print(f"{r['source']:<30}  {pinned_col:>10}  {actual_col:>10}  {dist_col:>8}  {status}")


def _render_markdown(rows: list[dict[str, object]]) -> None:
    """Print a markdown table to the console."""
    console.print("| Source | Pinned | Actual | Distance | Status |")
    console.print("|--------|--------|--------|----------|--------|")
    for r in rows:
        pinned_col = str(r["pinned"])[:8] if r["pinned"] else "-"
        actual_col = str(r["actual"])[:8] if r["actual"] else "-"
        dist_col = f"+{r['distance']}" if r["distance"] is not None else ("?" if r["drifted"] else "")
        status = "DRIFTED" if r["drifted"] else "clean"
        console.print(f"| {r['source']} | {pinned_col} | {actual_col} | {dist_col} | {status} |")
