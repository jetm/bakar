"""Parser for the bitbake-diffsigs ``Task dependencies changed`` list diff.

Foundation module: pure text parsing, no bakar imports. Extracted from
``commands/diffsigs.py`` so non-command callers can reuse it.
"""

from __future__ import annotations

import ast
import os
import re
from dataclasses import dataclass


def _extract_dep_diff(lines: list[str]) -> tuple[list[str], list[str]]:
    """Parse 'Task dependencies changed from: [...] to: [...]' and return (added, removed)."""
    text = "\n".join(lines)
    from_match = re.search(
        r"Task dependencies changed from:\s*(\[.*?\])\s*to:\s*(\[.*?\])\s*$", text, re.DOTALL | re.MULTILINE
    )
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


@dataclass(frozen=True)
class Cause:
    """One classified leaf cause of a signature change."""

    kind: str
    subject: str
    task: str


_CHAIN_RE = re.compile(r"Hash for task dependency (\S+) changed from \S+ to \S+")
_VALUE_RE = re.compile(r"Variable (\S+) value changed")
_VARDEPS_RE = re.compile(r"List of dependencies for variable (\S+) changed from")
_CHANGED_ITEMS_RE = re.compile(r"changed items:\s*frozenset\(\{(.*)\}\)")
_DEPVAR_RE = re.compile(r"Dependency on variable (\S+) was (added|removed)")
_FILE_DEP_RE = re.compile(r"Dependency on checksum of file (.+?) was ")
_FILE_SUM_RE = re.compile(r"Checksum for file (.+?) changed")
_TASKDEP_RE = re.compile(r"Dependency on task (\S+) was (added|removed)")
_TAINT_RE = re.compile(r"Taint \(by forced/invalidated task\) changed")
_UNRECOVERABLE_RE = re.compile(r"Unable to find matching sigdata for (\S+)")
_QUOTED_RE = re.compile(r"'([^']*)'")
_FLAG_RE = re.compile(r"^[^\[\]]+(\[[^\]]+\])$")


def _normalize_item(item: str) -> str:
    """Map ``X[flag]`` to ``*[flag]`` so one flag added to many variables is one subject."""
    m = _FLAG_RE.match(item)
    return f"*{m.group(1)}" if m else item


def _normalize_items(items: list[str]) -> str:
    return ",".join(sorted({_normalize_item(i) for i in items}))


def _classify(stripped: str, following: list[str]) -> tuple[Cause, int] | None:
    """Classify one non-chain line; return (kind/subject cause with empty task, lines consumed)."""
    if m := _VALUE_RE.search(stripped):
        return Cause("value", m.group(1), ""), 0
    if _VARDEPS_RE.search(stripped):
        for offset, nxt in enumerate(following, start=1):
            if not nxt.strip():
                continue
            ci = _CHANGED_ITEMS_RE.search(nxt)
            if ci:
                return Cause("vardeps", _normalize_items(_QUOTED_RE.findall(ci.group(1))), ""), offset
            break
        return Cause("vardeps", "", ""), 0
    if m := _DEPVAR_RE.search(stripped):
        return Cause(f"depvar-{m.group(2)}", m.group(1), ""), 0
    if m := _FILE_DEP_RE.search(stripped) or _FILE_SUM_RE.search(stripped):
        return Cause("file", os.path.basename(m.group(1)), ""), 0
    if m := _TASKDEP_RE.search(stripped):
        return Cause(f"taskdep-{m.group(2)}", m.group(1), ""), 0
    if _TAINT_RE.search(stripped):
        return Cause("taint", "", ""), 0
    if stripped.startswith("Task dependencies changed from"):
        added, removed = _extract_dep_diff([stripped, *following[:3]])
        parts = [f"+{a}" for a in added] + [f"-{r}" for r in removed]
        return Cause("taskvardeps", _normalize_items_signed(parts), ""), 0
    if m := _UNRECOVERABLE_RE.search(stripped):
        return Cause("unrecoverable", m.group(1), ""), 0
    return None


def _normalize_items_signed(parts: list[str]) -> str:
    return ",".join(sorted({p[0] + _normalize_item(p[1:]) for p in parts}))


#: Lines after a non-chain line that ``_classify`` may read (``Task dependencies changed`` list).
_LOOKAHEAD = 3


def _lookahead(lines: list[str], idx: int) -> list[str]:
    """The lines ``_classify`` can need after ``lines[idx]``, without copying the whole tail.

    That is the next ``_LOOKAHEAD`` lines, extended through the first non-blank
    line when they are all blank (a vardeps line reads its next non-blank line).
    """
    end = idx + 1 + _LOOKAHEAD
    window = lines[idx + 1 : end]
    if all(not ln.strip() for ln in window):
        while end < len(lines) and not lines[end - 1].strip():
            end += 1
        window = lines[idx + 1 : end]
    return window


def leaf_causes(lines: list[str], entry_task: str) -> list[Cause]:
    """Reduce ``compare_sigfiles`` output to classified leaf causes.

    A cause's task is the key of the deepest enclosing ``Hash for task
    dependency`` chain line, or ``entry_task`` when there is none. Chain lines
    are never leaves. Identical causes are returned once.
    """
    stack: list[tuple[int, str]] = []
    groups: dict[tuple[tuple[int, str], ...], tuple[str, list[tuple[int, str, list[str]]]]] = {}
    for idx, raw in enumerate(lines):
        stripped = raw.strip()
        if not stripped:
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        while stack and stack[-1][0] >= indent:
            stack.pop()
        chain = _CHAIN_RE.search(stripped)
        if chain:
            stack.append((indent, chain.group(1)))
            continue
        task = stack[-1][1] if stack else entry_task
        groups.setdefault(tuple(stack), (task, []))[1].append((indent, stripped, _lookahead(lines, idx)))

    out: list[Cause] = []
    for task, entries in groups.values():
        classified: list[Cause] = []
        basehash = False
        for _indent, stripped, following in entries:
            if _CHANGED_ITEMS_RE.search(stripped):
                continue
            if stripped.startswith("basehash changed from"):
                basehash = True
                continue
            res = _classify(stripped, following)
            if res:
                classified.append(Cause(res[0].kind, res[0].subject, task))
        if classified:
            out.extend(classified)
        elif basehash:
            out.append(Cause("basehash", "", task))
        else:
            min_indent = min(e[0] for e in entries)
            out.extend(Cause("unclassified", s[:160], task) for i, s, _ in entries if i == min_indent)
    return list(dict.fromkeys(out))
