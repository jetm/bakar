"""bakar cache subcommand - push downloads to the central store, report the queue, capture hangs.

Three verbs: ``push --watch`` (live push of finished downloads), ``status``
(push-queue summary) and ``capture`` (hang diagnostics for running BitBake).
Directories and the push target come from the loaded user config, with the
``DL_DIR`` and ``SSTATE_DIR`` environment variables taking precedence.
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
from bakar.steps import cache_capture, cache_push
from bakar.steps.cache_queue import PushQueue

declare_global_mode_exclusion(
    "cache",
    "pushes downloads over ssh, reads the push queue and captures process state; launches no kas",
)

PUSH_KEY = Path("~/.ssh/bakar-cache-push")

cache_app = typer.Typer(
    help="Push downloads to the central store, report the push queue, and capture hang diagnostics.",
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


app.add_typer(cache_app, name="cache")
