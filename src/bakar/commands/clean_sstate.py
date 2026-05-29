"""bakar clean-sstate - prune stale sstate-cache entries by age."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Annotated

import typer

import bakar.commands._app as _state
from bakar.commands._app import app, console


def _resolve_sstate_dir(override: Path | None) -> Path | None:
    """Return the effective SSTATE_DIR: CLI override > env var > user config."""
    if override is not None:
        return override
    env_val = os.environ.get("SSTATE_DIR")
    if env_val:
        return Path(env_val)
    cfg = _state._USER_CONFIG
    if cfg is not None and cfg.sstate_dir:
        return Path(cfg.sstate_dir)
    return None


def _atime_tracked(path: Path) -> bool:
    """Return True if the filesystem containing *path* tracks access times.

    Reads /proc/mounts and finds the longest (most specific) mount point
    that is a directory ancestor of *path*. Returns False when noatime is
    present in that mount's options, True otherwise.
    """
    try:
        mounts_text = Path("/proc/mounts").read_text(encoding="utf-8")
    except OSError:
        return False
    resolved = str(path.resolve())
    best_len = -1
    best_opts = ""
    for line in mounts_text.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        mp = parts[1]
        # Proper directory-prefix check: /home matches /home/user but not /homeother
        if resolved == mp or resolved.startswith(mp.rstrip("/") + "/"):
            if len(mp) > best_len:
                best_len = len(mp)
                best_opts = parts[3]
    return "noatime" not in best_opts.split(",")


def _fmt_size(n_bytes: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n_bytes < 1024:
            return f"{n_bytes:.1f} {unit}"
        n_bytes //= 1024
    return f"{n_bytes:.1f} PiB"


@app.command(name="clean-sstate")
def clean_sstate(
    older_than: Annotated[
        int,
        typer.Option("--older-than", help="Age threshold in days (default: 30)", min=1),
    ] = 30,
    sstate_dir: Annotated[
        Path | None,
        typer.Option("--sstate-dir", help="Override SSTATE_DIR path"),
    ] = None,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Skip confirmation prompt (for scripting)"),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", "-n", help="Scan and report without deleting or prompting"),
    ] = False,
) -> None:
    """Prune stale sstate-cache entries older than N days.

    Scans SSTATE_DIR, reports what would be removed, then prompts for
    confirmation. Pass --yes to skip the prompt. Pass --dry-run to scan
    without prompting or deleting (useful for scripting or inspection).

    Uses atime (last read) when the filesystem tracks it. On noatime mounts
    falls back to mtime (creation date), which is less precise: a file created
    60 days ago but reused yesterday would still be removed.

    SSTATE_DIR is resolved from the SSTATE_DIR env var, then from
    ~/.config/bakar/config.toml, then from --sstate-dir.
    """
    effective_dir = _resolve_sstate_dir(sstate_dir)
    if effective_dir is None:
        console.print(
            "[red]SSTATE_DIR not set.[/] Export it as an env var or add "
            "'sstate_dir = \"/path\"' under [build] in ~/.config/bakar/config.toml"
        )
        raise typer.Exit(code=2)
    if not effective_dir.is_dir():
        console.print(f"[red]SSTATE_DIR does not exist:[/] {effective_dir}")
        raise typer.Exit(code=2)

    use_atime = _atime_tracked(effective_dir)

    console.print(f"SSTATE_DIR : {effective_dir}")

    if use_atime:
        time_label = "atime (last read)"
        stat_attr = "st_atime"
    else:
        console.print(
            "[yellow]Warning:[/] noatime detected on this filesystem - "
            "access times are not tracked. Falling back to [bold]mtime (creation date)[/].\n"
            "Files created more than N days ago will be removed even if reused recently."
        )
        time_label = "mtime (creation date)"
        stat_attr = "st_mtime"

    console.print(f"Time basis : {time_label}")
    console.print(f"Threshold  : {older_than} days")
    console.print()

    cutoff = time.time() - older_than * 86400

    stale: list[Path] = []
    total_bytes = 0
    for f in effective_dir.rglob("*"):
        if not f.is_file(follow_symlinks=False):
            continue
        try:
            st = f.stat()
        except OSError:
            continue
        if getattr(st, stat_attr) < cutoff:
            stale.append(f)
            total_bytes += st.st_size

    if not stale:
        console.print(f"[green]Nothing to remove.[/] No files with {time_label} older than {older_than} days.")
        return

    console.print(f"Found [bold]{len(stale):,}[/] files totalling [bold]{_fmt_size(total_bytes)}[/]")

    if dry_run:
        console.print()
        console.print("Dry run - no files deleted.")
        return

    console.print()
    if not yes:
        confirmed = typer.confirm(f"Delete {len(stale):,} files ({_fmt_size(total_bytes)})?")
        if not confirmed:
            console.print("Aborted.")
            raise typer.Exit()

    removed = 0
    freed = 0
    candidate_dirs: set[Path] = set()
    for f in stale:
        try:
            sz = f.stat().st_size
            f.unlink()
            removed += 1
            freed += sz
            candidate_dirs.add(f.parent)
        except OSError:
            pass

    # Remove directories that became empty after deletion, deepest first.
    # Use a work-list so successfully removed parents are also visited.
    empty_dirs = 0
    pending = sorted(candidate_dirs, key=lambda p: len(p.parts), reverse=True)
    while pending:
        d = pending.pop(0)
        if d == effective_dir or d == effective_dir.parent:
            continue
        try:
            d.rmdir()
            empty_dirs += 1
            pending.append(d.parent)
            pending.sort(key=lambda p: len(p.parts), reverse=True)
        except OSError:
            pass

    console.print(f"[green]Deleted[/] {removed:,} files ([bold]{_fmt_size(freed)}[/] freed)")
    if empty_dirs:
        console.print(f"Removed {empty_dirs} empty directories")
