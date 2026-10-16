"""Shared interactive prompt helper for the init and presets wizards."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import typer

if TYPE_CHECKING:
    import questionary


def _ask(question: questionary.Question) -> str:
    """Call question.ask() and abort cleanly on None (questionary swallows Ctrl-C).

    questionary's ``.ask()`` is untyped, so the answer is cast to ``str``; every
    caller asks a text, path or select question.
    """
    result = question.ask()
    if result is None:
        raise typer.Abort
    return cast("str", result)
