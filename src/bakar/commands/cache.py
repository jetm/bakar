"""bakar cache subcommand - push downloads, report the queue, capture hangs, prune downloads, link mirrors.

Five verbs: ``push --watch`` (live push of finished downloads), ``status``
(push-queue summary), ``capture`` (hang diagnostics for running BitBake),
``prune-downloads`` (remove downloads the hub or a clone already holds; a dry
run unless ``--apply``) and ``link-mirror`` (link files held by the configured
mirror directories into the downloads directory; a dry run unless
``--apply``). Directories and the push target come from the loaded user
config, with the ``DL_DIR`` and ``SSTATE_DIR`` environment variables taking
precedence. The mirror directories come from the user config alone.
"""

from __future__ import annotations

import datetime
import os
from pathlib import Path
from typing import Annotated

import typer
from rich.markup import escape

import bakar.commands._app as _state
from bakar.commands._app import app, console
from bakar.commands._helpers import declare_global_mode_exclusion
from bakar.steps import cache_capture, cache_prune, cache_push, dl_link, hub_mount
from bakar.steps.cache_queue import PushQueue

declare_global_mode_exclusion(
    "cache",
    "pushes downloads over ssh, reads the push queue, captures process state and, with --apply, removes files from "
    "the local downloads directory and creates links in it; launches no kas",
)

PUSH_KEY = Path("~/.ssh/bakar-cache-push")

cache_app = typer.Typer(
    help=(
        "Push downloads to the central store, report the push queue, capture hang diagnostics, "
        "prune downloads and link mirror files."
    ),
    no_args_is_help=True,
)


def _dir(env_name: str, configured: str | None) -> Path | None:
    value = os.environ.get(env_name) or configured
    return Path(value).expanduser() if value else None


def _configured(attr: str) -> str | None:
    cfg = _state._USER_CONFIG
    return getattr(cfg, attr, None) if cfg is not None else None


@cache_app.command("push")
def push(
    watch: Annotated[
        bool,
        typer.Option("--watch", help="Watch the downloads directory and push finished files until interrupted."),
    ] = False,
) -> None:
    """Push finished downloads to the central store (requires --watch)."""
    if not watch:
        console.print("[red]bakar cache push only runs with --watch[/]")
        raise typer.Exit(code=2)
    dl_dir = _dir("DL_DIR", _configured("dl_dir"))
    target = _configured("cache_push_target")
    key = PUSH_KEY.expanduser()
    problem = cache_push.check_prerequisites(dl_dir, target, key)
    if problem is not None:
        console.print(f"[red]{escape(problem)}[/]")
        raise typer.Exit(code=1)
    assert dl_dir is not None
    assert target is not None
    mirror_url = os.environ.get("BAKAR_DL_MIRROR_URL") or _configured("dl_mirror_url") or None
    with PushQueue() as queue:
        try:
            cache_push.watch(dl_dir, target, key, queue=queue, mirror_url=mirror_url)
        except KeyboardInterrupt:
            pass


@cache_app.command("status")
def status() -> None:
    """Print the push-queue length, oldest entry age, pause state and last reconcile."""
    with PushQueue() as queue:
        snap = queue.snapshot()
    oldest = "-" if snap.oldest_age_s is None else str(int(snap.oldest_age_s))
    paused = f"yes ({snap.paused_reason})" if snap.paused else "no"
    if snap.last_reconcile is None:
        reconciled = "never"
    else:
        reconciled = datetime.datetime.fromtimestamp(snap.last_reconcile, tz=datetime.UTC).isoformat()
    typer.echo(f"queue: {snap.length}")
    typer.echo(f"oldest: {oldest}")
    typer.echo(f"paused: {paused}")
    typer.echo(f"last reconcile: {reconciled}")


@cache_app.command("capture")
def capture() -> None:
    """Capture BitBake process state and NFS/lock diagnostics for a hang."""
    dirs = [
        d
        for d in (
            _dir("DL_DIR", _configured("dl_dir")),
            _dir("SSTATE_DIR", _configured("sstate_dir")),
            _dir("CCACHE_DIR", _configured("ccache_dir")),
        )
        if d is not None
    ]
    typer.echo(cache_capture.render(cache_capture.capture(dirs)))


@cache_app.command("prune-downloads")
def prune_downloads(
    apply: Annotated[
        bool,
        typer.Option("--apply", help="Remove the listed items. Nothing is deleted without this flag."),
    ] = False,
    clone_older_than: Annotated[
        int | None,
        typer.Option(
            "--clone-older-than",
            min=1,
            help="Also plan git2 clones whose .done stamp (written at the last fetch) is older than this many days.",
        ),
    ] = None,
) -> None:
    """Remove downloads the hub already serves or that a complete git2 clone makes redundant."""
    dl_dir = _dir("DL_DIR", _configured("dl_dir"))
    if dl_dir is None or not dl_dir.is_dir():
        console.print("[red]no downloads directory: set DL_DIR or build.dl_dir to an existing directory[/]")
        raise typer.Exit(code=1)
    mirror_url = os.environ.get("BAKAR_DL_MIRROR_URL") or _configured("dl_mirror_url") or None
    plan = cache_prune.plan_prune(
        dl_dir,
        mirror_url=mirror_url,
        clone_older_than_days=clone_older_than,
        head=cache_prune.probes.head_info,
    )
    result = cache_prune.apply_plan(plan, dl_dir) if apply else None
    typer.echo(cache_prune.render_report(plan, result, apply))


@cache_app.command("link-mirror")
def link_mirror(
    apply: Annotated[
        bool,
        typer.Option("--apply", help="Create the links. Nothing is created without this flag."),
    ] = False,
) -> None:
    """Link files held by the mirror directories into the downloads directory."""
    dl_dir = _dir("DL_DIR", _configured("dl_dir"))
    if dl_dir is None:
        console.print("[red]no downloads directory: set DL_DIR or build.dl_dir to an existing directory[/]")
        raise typer.Exit(code=1)
    if not dl_dir.is_dir():
        console.print(f"[red]downloads path is not a directory: {escape(str(dl_dir))}[/]")
        raise typer.Exit(code=1)
    value = _configured("dl_mirror_paths")
    if not value:
        console.print("[red]no mirror directories: set build.dl_mirror_paths[/]")
        raise typer.Exit(code=1)
    if _configured("container") is True:
        console.print(
            "[red]the user configuration selects the container build; "
            "mirror directories are not used in a container build[/]"
        )
        raise typer.Exit(code=1)
    statuses = hub_mount.probe_dirs(hub_mount.parse_dirs(value))
    for status in statuses:
        if status.state != "ready":
            console.print(f"[yellow]skipping mirror directory {escape(str(status.path))}: {escape(status.state)}[/]")
    ready = [s.path for s in statuses if s.state == "ready"]
    if not ready:
        console.print("[red]no mirror directory is ready: check build.dl_mirror_paths[/]")
        raise typer.Exit(code=1)
    plan = dl_link.plan_links(dl_dir, ready)
    result = dl_link.apply_links(plan, dl_dir) if apply else dl_link.ApplyResult()
    typer.echo(dl_link.render_report(plan, result, apply=apply))


app.add_typer(cache_app, name="cache")
