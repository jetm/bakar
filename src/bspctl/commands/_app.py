"""Typer app and Rich console singletons shared across all bspctl subcommands."""

from __future__ import annotations

import typer
from rich.console import Console

app = typer.Typer(
    help="BSP orchestrator (NXP i.MX + TI Sitara built-in).",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)
console = Console(stderr=True)
