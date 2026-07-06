"""bakar clean-cache - prune stale sstate-cache and ccache entries by age.

With ``--full`` the command instead runs a total cold-reset of the sccache-dist
cluster and Yocto caches (ported from the former ``scripts/clean-all-cache.sh``):
it empties the shared sstate in place (NFS-safe), wipes each node's build dir and
the sccache client disk cache, stops the client daemon, then wipes and
reinitialises the sccache-dist server on every cluster node.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

import bakar.commands._app as _state
from bakar.commands._app import app, console
from bakar.commands._helpers import _find_workspace_from_cwd
from bakar.config import shared_ccache_dir
from bakar.fsremove import parallel_apply, parallel_rmtree

if TYPE_CHECKING:
    from collections.abc import Callable


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


def _stage_and_delete(stale_files: list[Path], effective_dir: Path) -> tuple[int, int]:
    """Move *stale_files* into a staging dir inside *effective_dir*, then delete them in parallel.

    Creates ``effective_dir / ".bakar-gc-<pid>"`` as a direct child of the sstate root so
    ``os.rename`` stays on one device (atomic).  Each file is renamed under a monotonic integer
    name to avoid basename collisions, which makes the cache namespace consistent before any
    bytes are freed.  Both the rename pass and the unlink pass run across a thread pool
    (:data:`bakar.fsremove.GC_WORKERS`) with a progress bar; ``os.rename``/``os.unlink`` release the GIL, so
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

    moved = [r for r in parallel_apply(list(enumerate(stale_files)), _stage, "Staging stale files") if r is not None]
    removed = len(moved)
    freed = sum(sz for _, sz in moved)

    def _unlink(dest: Path) -> None:
        try:
            os.unlink(dest)
        except OSError:
            pass

    parallel_apply([dest for dest, _ in moved], _unlink, "Freeing disk space")

    try:
        staging.rmdir()
    except OSError:
        shutil.rmtree(staging, ignore_errors=True)
    return removed, freed


# ---------------------------------------------------------------------------
# Full cold-reset (ports scripts/clean-all-cache.sh)
# ---------------------------------------------------------------------------

_DIST_STATUS_RETRIES = 3


def _log(msg: str) -> None:
    """Print one cold-reset progress line, mirroring the script's ``log`` output."""
    console.print(f"cold-reset: {msg}", highlight=False)


def _local_ips() -> set[str]:
    """Return this host's interface IPs via ``ip -o addr show`` (empty on failure).

    Column 4 of each ``ip -o addr`` row is ``<addr>/<prefix>``; the prefix is
    stripped so a server ``id`` can be matched against the bare address.
    """
    try:
        out = subprocess.run(
            ["ip", "-o", "addr", "show"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except FileNotFoundError, subprocess.TimeoutExpired:
        return set()
    ips: set[str] = set()
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 4:
            ips.add(parts[3].split("/", 1)[0])
    return ips


def _parse_dist_status_servers(status_json: str, local_ips: set[str]) -> list[str]:
    """Return the secondary server hosts named in a ``sccache --dist-status`` blob.

    Mirrors the bash python filter: reads ``SchedulerStatus[1].servers[].id``,
    strips the ``:port`` suffix, drops this host's own IPs, and dedupes while
    preserving order. Returns [] on malformed JSON or an unexpected shape.
    """
    try:
        status = json.loads(status_json)
    except ValueError, TypeError:
        return []
    sched = status.get("SchedulerStatus") if isinstance(status, dict) else None
    if not isinstance(sched, list) or len(sched) < 2 or not isinstance(sched[1], dict):
        return []
    servers = sched[1].get("servers") or []
    hosts: list[str] = []
    seen: set[str] = set()
    for srv in servers:
        if not isinstance(srv, dict):
            continue
        host = (srv.get("id") or "").rsplit(":", 1)[0]
        if host and host not in local_ips and host not in seen:
            seen.add(host)
            hosts.append(host)
    return hosts


def _resolve_secondaries() -> list[str]:
    """Resolve the secondary (non-local) sccache-dist build servers to reset.

    Precedence mirrors clean-all-cache.sh: an explicit ``SECONDARY_NODES`` env
    override (space-split) wins; otherwise the live server list reported by
    ``sccache --dist-status`` with this host's own IPs filtered out. ``--dist-status``
    routes through the local client daemon, which the first call auto-starts, so it
    is retried until the scheduler reports its ``servers``. Returns [] when sccache
    is absent or the scheduler never reports its servers.
    """
    override = os.environ.get("SECONDARY_NODES")
    if override:
        return override.split()
    if shutil.which("sccache") is None:
        return []
    status_json = ""
    for attempt in range(_DIST_STATUS_RETRIES):
        try:
            out = subprocess.run(
                ["sccache", "--dist-status"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except FileNotFoundError, subprocess.TimeoutExpired:
            return []
        status_json = out.stdout
        if '"servers"' in status_json:
            break
        if attempt < _DIST_STATUS_RETRIES - 1:
            time.sleep(1)
    if not status_json:
        return []
    return _parse_dist_status_servers(status_json, _local_ips())


def _resolve_build_dirs(override: Path | None) -> list[Path]:
    """Resolve the per-node build dir(s) to wipe.

    Precedence: ``--build-dir`` override, then the ``BUILD_DIR`` env var, then
    every ``build-*`` directory under the workspace found by walking up from CWD.
    Returns [] when none resolve, signalling the caller to skip the build-dir step.
    """
    if override is not None:
        return [override]
    env_val = os.environ.get("BUILD_DIR")
    if env_val:
        return [Path(env_val)]
    ws = _find_workspace_from_cwd()
    if ws is None:
        return []
    return sorted(p for p in ws.glob("build-*") if p.is_dir())


def _empty_dir_in_place(path: Path) -> None:
    """Delete every child of *path* but keep *path* itself (NFS-safe).

    Removing and recreating an NFS-exported directory swaps its inode and breaks
    every client mount (the secondary sees ESTALE). Emptying its contents in place
    cools the shared cache for all nodes while preserving the export root. Creates
    *path* first when absent - bakar doctor needs it to exist.
    """
    path.mkdir(parents=True, exist_ok=True)
    for child in path.iterdir():
        try:
            if child.is_symlink() or not child.is_dir():
                child.unlink()
            else:
                shutil.rmtree(child, ignore_errors=True)
        except OSError:
            pass


def _sccache_client_cache_dirs() -> list[Path]:
    """Return the sccache client disk-cache dirs under ``~/.cache`` to wipe."""
    cache = Path.home() / ".cache"
    dirs = [cache / "sccache", cache / "sccache-dist-client"]
    dirs.extend(sorted(cache.glob("sccache-dist-client.stale.*")))
    return dirs


def _reset_dist_server_cmd() -> str:
    """Return the shell command that wipes and reinitialises the sccache-dist server.

    The build server does a NON-recursive ``mkdir`` of ``build/toolchains/<hash>``
    per job, so ``build/toolchains`` must exist or every distributed compile fails
    with "failed to prepare overlay dirs" (HTTP 500). Recreate that subdir
    explicitly and restart the service so its in-memory toolchain refs match the
    wiped disk.
    """
    return (
        "sudo rm -rf /var/cache/sccache-dist/toolchains /var/cache/sccache-dist/build "
        "&& sudo mkdir -p /var/cache/sccache-dist/toolchains /var/cache/sccache-dist/build/toolchains "
        "&& sudo systemctl restart sccache-server"
    )


def _remote_reset_cmd(build_dirs: list[Path], reset_cmd: str) -> str:
    """Build the ssh command run on a secondary: wipe its build dir(s), then reset.

    The secondary runs its own bitbake into a per-node (not shared) build dir, so a
    cold multi-node run must clear it too. The shared SSTATE_DIR is NOT re-wiped
    remotely - the local in-place empty already cooled the single NFS copy. Each
    build dir path is shell-quoted: an unquoted path containing spaces or shell
    metacharacters would otherwise break or reinterpret the remote command.
    """
    prefix = "".join(f"rm -rf {shlex.quote(str(b))}; " for b in build_dirs)
    return prefix + reset_cmd


def _run_full_reset(
    sstate_override: Path | None,
    build_dir_override: Path | None,
    yes: bool,
    dry_run: bool,
) -> None:
    """Total cold-reset of the sccache-dist cluster and Yocto caches.

    Faithful port of scripts/clean-all-cache.sh. Resolves the shared sstate dir
    and the per-node build dir(s), then builds one ordered list of (description,
    thunk) actions covering every step. ``dry_run`` prints each action's
    description without calling any thunk; a real run calls each thunk in order.
    Sharing one list between the two modes keeps the printed plan and the actual
    execution from drifting apart. Resolving the secondary build servers requires
    a live ``sccache --dist-status`` call, so that resolution is itself deferred
    into its own thunk - a ``--dry-run`` must never trigger it.
    """
    sstate = _resolve_sstate_dir(sstate_override)
    if sstate is None:
        console.print(
            "[red]SSTATE_DIR not set.[/] Export it as an env var or add "
            "'sstate_dir = \"/path\"' under [build] in ~/.config/bakar/config.toml"
        )
        raise typer.Exit(code=2)
    build_dirs = _resolve_build_dirs(build_dir_override)
    reset_cmd = _reset_dist_server_cmd()
    cache_dirs = _sccache_client_cache_dirs()
    # The bitbake-prserv binary lives under the workspace root (sources/poky/bitbake,
    # sources/bitbake, or a sibling bitbake/ - see hashserv._find_binary), not inside a
    # build-* dir, so a workspace-root binary_root is required for the nxp/ti cluster
    # layout this reset targets; build_dirs[0]/cwd is a last-resort fallback only.
    binary_root = _find_workspace_from_cwd() or (build_dirs[0] if build_dirs else Path.cwd())
    user_cfg = _state._USER_CONFIG
    bind_host = (user_cfg.cluster_bind_host if user_cfg is not None else None) or "localhost"

    def _stop_daemons() -> None:
        # Lazy import to avoid any future import cycle if hashserv/prserv grow deps.
        from bakar import hashserv, prserv

        _log("stopping hashserv/prserv daemons (if running) ...")
        hashserv.stop(sstate)
        prserv.stop(sstate, binary_root=binary_root, bind_host=bind_host)

    def _empty_sstate() -> None:
        _log("emptying shared sstate in place (can take a while on a large cache) ...")
        _empty_dir_in_place(sstate)
        _log("shared sstate emptied")

    def _wipe_build_dir(b: Path) -> None:
        _log(f"wiping local build dir {b} ...")
        parallel_rmtree(b, description=f"Removing {b.name}/")

    def _wipe_cache_dir(c: Path) -> None:
        _log(f"wiping sccache client disk cache {c} ...")
        shutil.rmtree(c, ignore_errors=True)

    def _stop_client_daemon() -> None:
        _log("stopping the sccache client daemon (if running) ...")
        try:
            subprocess.run(["pkill", "-f", "^/usr/bin/sccache$"], check=False)
        except FileNotFoundError:
            pass

    def _reset_pc1() -> None:
        _log("resetting sccache-dist server on PC1 (local) ...")
        subprocess.run(["bash", "-c", reset_cmd], check=False)
        _log("PC1 sccache-dist server reset + restarted")

    def _reset_secondaries() -> None:
        secondaries = _resolve_secondaries()
        if not secondaries:
            _log("no secondary build servers detected; reset PC1 only.")
            return
        for node in secondaries:
            _log(f"resetting {node}: local build dir + sccache-dist server ...")
            subprocess.run(["ssh", "-t", node, _remote_reset_cmd(build_dirs, reset_cmd)], check=False)
            _log(f"{node} reset complete")

    actions: list[tuple[str, Callable[[], None]]] = [
        ("stop hashserv/prserv daemons (if running)", _stop_daemons),
        (f"empty in place (keep root): {sstate}", _empty_sstate),
    ]
    actions.extend((f"rm -rf (local): {b}", lambda b=b: _wipe_build_dir(b)) for b in build_dirs)
    actions.extend((f"rm -rf (local): {c}", lambda c=c: _wipe_cache_dir(c)) for c in cache_dirs)
    actions.append(("pkill -f '^/usr/bin/sccache$'", _stop_client_daemon))
    actions.append((f"PC1 (local): {reset_cmd}", _reset_pc1))
    actions.append(
        ("reset secondary build servers (resolved via sccache --dist-status at run time)", _reset_secondaries)
    )

    _log("cold-reset starting")
    _log(f"shared SSTATE_DIR  = {sstate}")
    if build_dirs:
        _log("per-node build dir(s) = " + ", ".join(str(b) for b in build_dirs))
    else:
        _log("per-node build dir(s) = <none resolved; build-dir wipe skipped>")

    if dry_run:
        console.print()
        console.print("[bold]Dry run - no changes made.[/] Plan:", highlight=False)
        for desc, _thunk in actions:
            console.print(f"  {desc}", highlight=False)
        return

    if not yes:
        confirmed = typer.confirm(
            f"Cold-reset: empty {sstate}, wipe {len(build_dirs)} build dir(s) + the sccache "
            f"client cache, stop the client daemon, and reset the sccache-dist server on PC1 "
            f"and any secondary build servers. Proceed?"
        )
        if not confirmed:
            console.print("Aborted.")
            raise typer.Exit

    for _desc, thunk in actions:
        thunk()

    _log("cold-reset complete")


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
    build_dir: Annotated[
        Path | None,
        typer.Option("--build-dir", help="Override the per-node build dir wiped by --full"),
    ] = None,
    full: Annotated[
        bool,
        typer.Option(
            "--full",
            help="Total cold-reset of the sccache-dist cluster and caches (ignores --older-than)",
        ),
    ] = False,
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

    --full instead runs a total cold-reset of the sccache-dist cluster and
    caches: it empties the shared sstate in place, wipes each node's build dir
    and the sccache client cache, stops the client daemon, and resets the
    sccache-dist server on every cluster node. --full ignores the age-based
    prune options (--older-than / --sstate / --ccache).
    """
    if full:
        console.print(
            "[yellow]--full:[/] total cold-reset; the age-based prune options "
            "(--older-than / --sstate / --ccache) are ignored.",
        )
        _run_full_reset(sstate_dir, build_dir, yes, dry_run)
        return

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
