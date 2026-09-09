"""bakar sstate-seed subcommand - populate and inspect the native sstate seed.

Seeding the native/cross toolchain is the largest measured build-time lever on
this fleet (26.4 min to 9.0 min on PC3, 65% of wall-clock), and until now it
lived entirely outside bakar: a personal script plus a hand-typed
``sstate_mirrors`` string in one user's config. This command is the discoverable
form of both halves.

The seed is keyed by oe-core release codename, so ``--status`` is the part worth
running even when nothing is being populated: a seed built for another release is
inert rather than wrong, and an inert seed looks exactly like a configured one
until someone compares build times.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

import bakar.commands._app as _state
from bakar.commands._app import app, console
from bakar.commands._helpers import _find_workspace_from_cwd
from bakar.sstate_seed import (
    populate_seed,
    read_seed_marker,
    resolve_seed_for_workspace,
    seed_mirror_line,
)


def _human(size: int) -> str:
    """Render a byte count for a status line."""
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GiB"


@app.command("sstate-seed")
def sstate_seed(
    status: Annotated[
        bool,
        typer.Option("--status", help="Report the seed for this workspace without writing anything."),
    ] = False,
    source: Annotated[
        Path | None,
        typer.Option("--source", help="sstate directory to populate from. Defaults to the configured sstate_dir."),
    ] = None,
    workspace_override: Annotated[
        Path | None,
        typer.Option(
            "--workspace",
            help="Tree to read the oe-core release from. Use when the seed's owning checkout is not a bakar workspace.",
        ),
    ] = None,
) -> None:
    """Populate or inspect the native/cross sstate seed for this workspace's release."""
    # A seed belongs to an oe-core RELEASE, and the tree that owns one is not
    # always a bakar workspace - the benchmark checkout carries oe-core and none
    # of the workspace markers, and it owns the seed that actually pays off.
    # Requiring workspace detection there would leave the one seed worth having
    # unnameable, so --workspace names any oe-core-bearing tree directly.
    if workspace_override is not None:
        workspace = workspace_override
        if not (workspace / "openembedded-core" / "meta" / "conf" / "layer.conf").is_file():
            console.print(
                f"[red]no oe-core under {workspace}[/]; --workspace must name a tree containing openembedded-core/."
            )
            raise typer.Exit(code=1)
    else:
        found = _find_workspace_from_cwd()
        if found is None:
            console.print(
                "[red]not inside a bakar workspace[/]; run this from a workspace directory, or pass --workspace."
            )
            raise typer.Exit(code=1)
        workspace = found

    cfg_sstate = _state._USER_CONFIG.sstate_dir if _state._USER_CONFIG is not None else None
    if not cfg_sstate:
        console.print("[red]no sstate_dir configured[/]; set [build] sstate_dir in ~/.config/bakar/config.toml.")
        raise typer.Exit(code=1)

    seed_dir, release_key = resolve_seed_for_workspace(workspace, cfg_sstate)
    shown_release = release_key or "unknown"

    if status:
        marker = read_seed_marker(seed_dir)
        console.print(f"release:  {shown_release}")
        console.print(f"seed dir: {seed_dir}")
        if marker is None:
            # Absent, pre-marker, and corrupt are one answer to the only
            # question asked here: this seed cannot say what it was built for.
            console.print("[yellow]no seed recorded[/] - populate it, or it was written before markers existed.")
        else:
            console.print(f"objects:  {marker.files} ({_human(marker.total_bytes)})")
            console.print(f"built from: {marker.source_dir}")
            if marker.release_key != release_key:
                # Not an error: the objects simply never match, so the build
                # rebuilds what it should have restored and nothing says so.
                console.print(
                    f"[red]stale[/]: seed was built for {marker.release_key or 'unknown'}, "
                    f"workspace is {shown_release} - it will never hit."
                )
        console.print("\nSSTATE_MIRRORS line for this seed:")
        console.print(f"  {seed_mirror_line(seed_dir)}", soft_wrap=True, highlight=False)
        return

    source_dir = source if source is not None else Path(cfg_sstate)
    console.print(f"seeding {shown_release} from {source_dir}")
    result = populate_seed(source_dir, seed_dir, release_key=release_key)

    if result.source_missing:
        # Distinguished from an empty source on purpose: populating before the
        # first build is a sequencing mistake, and it must not read as a seed
        # that was written and found nothing.
        console.print(f"[red]source sstate directory does not exist[/]: {source_dir}")
        raise typer.Exit(code=1)
    if result.files == 0:
        console.print("[yellow]source was read and held no native/cross objects[/] - has a build completed?")
        return

    console.print(f"seeded {result.files} files ({_human(result.total_bytes)}) into {seed_dir}")
    console.print("\nSSTATE_MIRRORS line for this seed:")
    console.print(f"  {seed_mirror_line(seed_dir)}", soft_wrap=True, highlight=False)
