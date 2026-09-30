"""Parser for the bitbake-diffsigs ``Task dependencies changed`` list diff.

Foundation module: pure text parsing, no bakar imports. Extracted from
``commands/diffsigs.py`` so non-command callers can reuse it.
"""

from __future__ import annotations

import ast
import re


def _extract_dep_diff(lines: list[str]) -> tuple[list[str], list[str]]:
    """Parse 'Task dependencies changed from: [...] to: [...]' and return (added, removed)."""
    text = "\n".join(lines)
    from_match = re.search(r"Task dependencies changed from:\s*(\[.*?\])\s*to:\s*(\[.*?\])", text, re.DOTALL)
    if not from_match:
        return [], []
    try:
        from_list: list[str] = ast.literal_eval(from_match.group(1))
        to_list: list[str] = ast.literal_eval(from_match.group(2))
    except ValueError, SyntaxError:
        return [], []
    from_set, to_set = set(from_list), set(to_list)
    added = sorted(to_set - from_set)
    removed = sorted(from_set - to_set)
    return added, removed


def _recipe_from_task(task: str) -> str:
    """Return the recipe portion of a 'recipe:do_task' string."""
    return task.split(":")[0] if ":" in task else task
