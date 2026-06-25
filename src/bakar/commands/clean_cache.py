"""bakar clean-cache - prune stale sstate-cache and ccache entries by age."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Annotated

import typer

import bakar.commands._app as _state
from bakar.commands._app import app, console
from bakar.commands._helpers import _find_workspace_from_cwd
from bakar.config import shared_ccache_dir


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


def _resolve_ccache_dir(override: Path | None) -> Path | None:
    """Return the effective ccache dir: CLI override > config shared/explicit > workspace.

    Mirrors :attr:`bakar.config.BuildConfig.effective_ccache_dir`: an explicit
    ``[build] ccache_dir`` or ``ccache_shared`` selects a shared location;
    otherwise the per-workspace ``<workspace>/ccache`` is used, found by walking
    up from CWD. Returns None when none of these resolve (not in a workspace and
    no shared cache configured), signalling the caller to skip ccache.
    """
    if override is not None:
        return override
    cfg = _state._USER_CONFIG
    shared = shared_ccache_dir(cfg.ccache_dir, ccache_shared=cfg.ccache_shared) if cfg is not None else None
    if shared is not None:
        return shared
    ws = _find_workspace_from_cwd()
    return ws / "ccache" if ws is not None else None


def _atime_tracked(path: Path) -> bool:
    """Return True only if the filesystem containing *path* records reliable atimes.

    Reads /proc/mounts and finds the longest (most specific) mount point
    that is a directory ancestor of *path*. Returns False when the mount uses
    ``noatime`` (atime never updated) or ``relatime`` (atime updated at most
    once per 24h and trivially clobbered by any full-tree read - a backup, du,
    or file indexer resets every file's atime at once). Returns True otherwise
    (e.g. ``strictatime``). Only strict atime is a dependable last-read signal
    for age-based eviction.
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
    opts = best_opts.split(",")
    return "noatime" not in opts and "relatime" not in opts


def _fmt_size(n_bytes: int) -> str:
    from bakar.fmt import fmt_bytes_iec

    return fmt_bytes_iec(n_bytes)


def _scan_stale_files(effective_dir: Path, stat_attr: str, cutoff_ts: float) -> tuple[list[Path], int]:
    """Return (stale_files, total_stale_bytes) for entries older than cutoff_ts.

    Uses stat_attr ('st_atime' or 'st_mtime') for the age comparison.
    """
    stale: list[Path] = []
    total = 0
    for f in effective_dir.rglob("*"):
        if not f.is_file(follow_symlinks=False):
            continue
        try:
            st = f.stat()
            ts = getattr(st, stat_attr)
        except OSError:
            continue
        if ts < cutoff_ts:
            stale.append(f)
            total += st.st_size
    return stale, total


def _ccache_size_kib(ccache_dir: Path) -> int | None:
    """Current cache size in KiB via ``ccache --print-stats``, or None on failure."""
    try:
        out = subprocess.run(
            ["ccache", "--print-stats"],
            capture_output=True,
            text=True,
            timeout=10,
            env={**os.environ, "CCACHE_DIR": str(ccache_dir)},
            check=False,
        )
    except FileNotFoundError, subprocess.TimeoutExpired:
        return None
    if out.returncode != 0:
        return None
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == "cache_size_kibibyte":
            try:
                return int(parts[1])
            except ValueError:
                return None
    return None


def _ccache_evict(ccache_dir: Path, days: int) -> bool:
    """Run ``ccache --evict-older-than <days>d``; return True on success.

    ccache maintains its own index, manifests, and stats, so age-based pruning
    must go through ccache itself - deleting files by hand corrupts that state.
    """
    try:
        out = subprocess.run(
            ["ccache", "--evict-older-than", f"{days}d"],
            capture_output=True,
            text=True,
            timeout=120,
            env={**os.environ, "CCACHE_DIR": str(ccache_dir)},
            check=False,
        )
    except FileNotFoundError, subprocess.TimeoutExpired:
        return False
    return out.returncode == 0


def _delete_stale(stale: list[Path], effective_dir: Path) -> tuple[int, int, int]:
    """Delete *stale* files and prune emptied directories. Returns (removed, freed, empty_dirs)."""
    removed, freed = _stage_and_delete(stale, effective_dir)

    # Only consider parents of files that were actually removed; files skipped by
    # _stage_and_delete (OSError) keep their parent dirs occupied.
    candidate_dirs: set[Path] = {f.parent for f in stale if not f.exists()}

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
    return removed, freed, empty_dirs


# Thread-pool size for bulk sstate rename/unlink. These are I/O-bound syscalls
# that release the GIL, so a small pool parallelizes real disk work. Capped low
# because XFS serializes metadata per allocation group - past a handful of
# threads, per-AG lock contention on one device cancels the gain.
_GC_WORKERS = min(8, (os.cpu_count() or 4) * 2)


def _parallel_apply(items: list, fn, description: str) -> list:
    """Map *fn* over *items* across a thread pool, driving a Rich progress bar.

    Splits *items* into ``_GC_WORKERS`` round-robin chunks so only a handful of
    futures exist regardless of item count, then runs each chunk on a worker
    thread. ``Progress.advance`` is thread-safe, so progress ticks as each item
    completes. Results are returned in chunk order (callers do not rely on it).
    """
    if not items:
        return []
    from concurrent.futures import ThreadPoolExecutor

    from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeRemainingColumn

    workers = min(_GC_WORKERS, len(items))
    chunks = [items[w::workers] for w in range(workers)]
    results: list = []
    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeRemainingColumn(),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task(description, total=len(items))

        def _run_chunk(chunk: list) -> list:
            out = []
            for item in chunk:
                out.append(fn(item))
                progress.advance(task)
            return out

        with ThreadPoolExecutor(max_workers=workers) as ex:
            for chunk_result in ex.map(_run_chunk, chunks):
                results.extend(chunk_result)
    return results


def _stage_and_delete(stale_files: list[Path], effective_dir: Path) -> tuple[int, int]:
    """Move *stale_files* into a staging dir inside *effective_dir*, then delete them in parallel.

    Creates ``effective_dir / ".bakar-gc-<pid>"`` as a direct child of the sstate root so
    ``os.rename`` stays on one device (atomic).  Each file is renamed under a monotonic integer
    name to avoid basename collisions, which makes the cache namespace consistent before any
    bytes are freed.  Both the rename pass and the unlink pass run across a thread pool
    (:data:`_GC_WORKERS`) with a progress bar; ``os.rename``/``os.unlink`` release the GIL, so
    the pool parallelizes real disk work.  Files that fail to move/delete (``OSError``) are
    skipped, not fatal.

    Returns ``(removed, freed)`` where *removed* is the count of successfully moved files and
    *freed* is the sum of pre-move ``st_size`` values for those files.
    """
    staging = effective_dir / f".bakar-gc-{os.getpid()}"
    staging.mkdir(parents=False, exist_ok=True)

    def _stage(arg: tuple[int, Path]) -> tuple[Path, int] | None:
        i, f = arg
        dest = staging / str(i)
        try:
            sz = f.stat().st_size
            os.rename(f, dest)
        except OSError:
            return None
        else:
            return dest, sz

    moved = [r for r in _parallel_apply(list(enumerate(stale_files)), _stage, "Staging stale files") if r is not None]
    removed = len(moved)
    freed = sum(sz for _, sz in moved)

    def _unlink(dest: Path) -> None:
        try:
            os.unlink(dest)
        except OSError:
            pass

    _parallel_apply([dest for dest, _ in moved], _unlink, "Freeing disk space")

    try:
        staging.rmdir()
    except OSError:
        shutil.rmtree(staging, ignore_errors=True)
    return removed, freed


@app.command(name="clean-cache")
def clean_cache(
    older_than: Annotated[
        int,
        typer.Option("--older-than", help="Age threshold in days (default: 30)", min=1),
    ] = 30,
    sstate_dir: Annotated[
        Path | None,
        typer.Option("--sstate-dir", help="Override SSTATE_DIR path"),
    ] = None,
    ccache_dir: Annotated[
        Path | None,
        typer.Option("--ccache-dir", help="Override the ccache directory"),
    ] = None,
    sstate: Annotated[
        bool,
        typer.Option("--sstate/--no-sstate", help="Prune the sstate cache (default: on)"),
    ] = True,
    ccache: Annotated[
        bool,
        typer.Option("--ccache/--no-ccache", help="Evict the ccache (default: on)"),
    ] = True,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Skip confirmation prompt (for scripting)"),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", "-n", help="Scan and report without deleting or prompting"),
    ] = False,
) -> None:
    """Prune sstate and ccache entries older than N days.

    sstate is pruned by removing files older than the threshold (atime only on
    strictatime mounts, mtime on relatime/noatime mounts where atime is not a
    reliable last-read signal). ccache is pruned with
    ``ccache --evict-older-than Nd`` - ccache keeps its own index, so age-based
    eviction must go through ccache itself. Restrict to one cache with
    --no-sstate / --no-ccache.

    SSTATE_DIR is resolved from --sstate-dir, then the SSTATE_DIR env var, then
    ~/.config/bakar/config.toml. The ccache directory is resolved from
    --ccache-dir, then [build] ccache_shared / ccache_dir, then the current
    workspace's per-workspace cache.
    """
    if not sstate and not ccache:
        console.print("[yellow]Nothing to do[/] (both --no-sstate and --no-ccache).")
        raise typer.Exit

    # --- plan sstate ---
    sstate_ok = False
    sstate_effective: Path | None = None
    stale: list[Path] = []
    sstate_total = 0
    if sstate:
        sstate_effective = _resolve_sstate_dir(sstate_dir)
        if sstate_effective is None:
            console.print(
                "[red]SSTATE_DIR not set.[/] Export it as an env var or add "
                "'sstate_dir = \"/path\"' under [build] in ~/.config/bakar/config.toml"
            )
        elif not sstate_effective.is_dir():
            console.print(f"[red]SSTATE_DIR does not exist:[/] {sstate_effective}")
        else:
            use_atime = _atime_tracked(sstate_effective)
            console.print(f"SSTATE_DIR : {sstate_effective}")
            if use_atime:
                time_label = "atime (last read)"
            else:
                console.print(
                    "[yellow]Warning:[/] this filesystem is mounted relatime or noatime, "
                    "so access times are not a reliable last-read signal (a backup or indexer "
                    "pass resets them). Falling back to [bold]mtime (creation date)[/].\n"
                    "Files created more than N days ago will be removed even if reused recently."
                )
                time_label = "mtime (creation date)"
            console.print(f"Time basis : {time_label}")
            console.print(f"Threshold  : {older_than} days")
            stat_attr = "st_atime" if use_atime else "st_mtime"
            cutoff_ts = time.time() - older_than * 86_400
            stale, sstate_total = _scan_stale_files(sstate_effective, stat_attr, cutoff_ts)
            if stale:
                console.print(
                    f"sstate     : [bold]{len(stale):,}[/] files older than {older_than} days, "
                    f"totalling [bold]{_fmt_size(sstate_total)}[/]"
                )
            else:
                console.print(f"[green]sstate: Nothing to remove.[/] No files older than {older_than} days.")
            sstate_ok = True

    # --- plan ccache ---
    ccache_ok = False
    ccache_effective: Path | None = None
    ccache_before: int | None = None
    if ccache:
        ccache_effective = _resolve_ccache_dir(ccache_dir)
        if ccache_effective is None:
            console.print(
                "[yellow]ccache:[/] no cache directory (not in a workspace and no "
                "[build] ccache_shared / ccache_dir set); skipping. Pass --ccache-dir to target one."
            )
        elif not ccache_effective.is_dir():
            console.print(f"[yellow]ccache:[/] {ccache_effective} absent; skipping.")
        elif shutil.which("ccache") is None:
            console.print("[yellow]ccache:[/] binary not on PATH; skipping.")
        else:
            ccache_before = _ccache_size_kib(ccache_effective)
            size_note = f" ({_fmt_size(ccache_before * 1024)})" if ccache_before is not None else ""
            console.print(f"ccache     : {ccache_effective}{size_note}")
            ccache_ok = True

    if not sstate_ok and not ccache_ok:
        raise typer.Exit(code=2)

    if dry_run:
        console.print()
        console.print(f"Dry run - no changes made (would evict ccache entries older than {older_than} days).")
        return

    actions: list[str] = []
    if sstate_ok and stale:
        actions.append(f"delete {len(stale):,} sstate files ({_fmt_size(sstate_total)})")
    if ccache_ok:
        actions.append(f"evict ccache entries older than {older_than} days")
    if not actions:
        return

    console.print()
    if not yes:
        confirmed = typer.confirm("Proceed to " + " and ".join(actions) + "?")
        if not confirmed:
            console.print("Aborted.")
            raise typer.Exit

    if sstate_ok and stale and sstate_effective is not None:
        removed, freed, empty_dirs = _delete_stale(stale, sstate_effective)
        console.print(f"[green]sstate: deleted[/] {removed:,} files ([bold]{_fmt_size(freed)}[/] freed)")
        if empty_dirs:
            console.print(f"sstate: removed {empty_dirs} empty directories")

    if ccache_ok and ccache_effective is not None:
        if _ccache_evict(ccache_effective, older_than):
            after = _ccache_size_kib(ccache_effective)
            if ccache_before is not None and after is not None:
                freed_str = _fmt_size(max(0, ccache_before - after) * 1024)
                console.print(
                    f"[green]ccache: evicted[/] entries older than {older_than} days ([bold]{freed_str}[/] freed)"
                )
            else:
                console.print(f"[green]ccache: evicted[/] entries older than {older_than} days")
        else:
            console.print("[red]ccache: eviction failed[/] (see ccache output)")
