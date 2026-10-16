"""Shared interactive prompt helper for the init and presets wizards."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import typer

if TYPE_CHECKING:
    import questionary


def _ask(question: questionary.Question) -> Any:
    """Call question.ask() and abort cleanly on None (questionary swallows Ctrl-C).

    questionary's ``.ask()`` is untyped and its answer type depends on the
    prompt: ``str`` for text, path and select questions, ``bool`` for confirm.
    The result is therefore ``Any``; callers annotate the target they assign to.
    """
    result = question.ask()
    if result is None:
        raise typer.Abort
    return result
