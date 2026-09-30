"""Why did native/cross recipes rebuild? Attribute each rebuild to a signature change.

For every native or cross recipe that actually executed in a run, this pairs the
signature the run built with the newest earlier signature of the same recipe and
task, hands every pair to ``sigdiff_helper`` in ONE subprocess (bitbake's own
``bb.siggen.compare_sigfiles`` does the comparing), and groups the resulting
causes across recipes. Every rebuilt recipe lands in exactly one of four
buckets - attributed, not-recoverable, no-previous, unchanged - so the counts
always reconcile to the number of rebuilt recipes.

Like :mod:`bakar.insights_sstate` this is a report function returning frozen
dataclasses; rendering belongs to the command. Filesystem reads and the one
helper subprocess are the only side effects.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

from bakar import native_ledger
from bakar.insights_timing import correlation_window
from bakar.sigdiff_parse import Cause, leaf_causes

NO_DATA_MESSAGE = "every native and cross task was restored or already current"

ERR_NO_MANIFEST = "this run has no native signature manifest (it predates signature capture)"

REASON_NO_CURRENT = "no current signature captured"
REASON_CURRENT_MISSING = "current signature file not found"
REASON_NO_WINDOW = "no run window"
REASON_SCAN_INCOMPLETE = "scan incomplete"
REASON_NO_PREVIOUS = "no earlier signature found"
REASON_NO_RESULT = "helper returned no result"
REASON_NO_DIFFERENCE = "comparison reported no differences"

_SCAN_BUDGET_SECONDS = 120.0
_MAX_EXAMPLES = 5
_ENTRY_TASK = "do_populate_sysroot"


@dataclass(frozen=True)
class CauseGroup:
    """One cause shared by ``recipes`` recipes; ``examples`` holds up to 5 ``(recipe, task)``."""

    kind: str
    subject: str
    recipes: int
    examples: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class ReasonGroup:
    """Recipes sharing one explanation for landing in a non-attributed bucket."""

    reason: str
    recipes: int
    examples: tuple[str, ...]


@dataclass(frozen=True)
class NativesReport:
    """Attribution of a run's native/cross rebuilds; ``error`` set means no attribution was made."""

    executed_tasks: int = 0
    restored: int = 0
    rebuilt_recipes: int = 0
    attributed: int = 0
    not_recoverable: int = 0
    no_previous: int = 0
    unchanged: int = 0
    groups: tuple[CauseGroup, ...] = ()
    not_recoverable_groups: tuple[ReasonGroup, ...] = ()
    no_previous_groups: tuple[ReasonGroup, ...] = ()
    message: str = ""
    error: str = ""

    @property
    def reconciled(self) -> bool:
        """True when the four buckets account for every rebuilt recipe."""
        return self.attributed + self.not_recoverable + self.no_previous + self.unchanged == self.rebuilt_recipes


@dataclass
class _Pending:
    recipe: str
    task: str
    hash_: str
    new: Path
    old: Path


@dataclass
class _Buckets:
    not_recoverable: dict[str, list[str]] = field(default_factory=dict)
    no_previous: dict[str, list[str]] = field(default_factory=dict)
    unchanged: list[str] = field(default_factory=list)


def _reason_groups(mapping: dict[str, list[str]]) -> tuple[ReasonGroup, ...]:
    ordered = sorted(mapping.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    return tuple(ReasonGroup(r, len(v), tuple(v[:_MAX_EXAMPLES])) for r, v in ordered)


def _num(value: object) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _load_manifest(path: Path) -> dict[tuple[str, str], str] | None:
    """Map ``(recipe, task)`` to the manifest hash; None when absent or unreadable."""
    try:
        data = json.loads(path.read_text())
    except OSError, ValueError:
        return None
    tasks = data.get("tasks") if isinstance(data, dict) else None
    if not isinstance(tasks, list):
        return None
    out: dict[tuple[str, str], str] = {}
    for item in tasks:
        if not isinstance(item, dict):
            continue
        recipe, task, hash_ = item.get("recipe"), item.get("task"), item.get("hash")
        if native_ledger.valid_recipe(recipe) and native_ledger.valid_task(task) and native_ledger.valid_hash(hash_):
            out[(str(recipe), str(task))] = str(hash_)
    return out


def _entry_tasks(rows: list[Any]) -> dict[str, list[str]]:
    """Per recipe, its executed tasks ordered by preference: sysroot first, then latest completed."""
    per: dict[str, list[tuple[float, str]]] = {}
    for row in rows:
        recipe, task = native_ledger.row_pn(row), str(row.get("task") or "")
        if recipe and task:
            per.setdefault(recipe, []).append((_num(row.get("completed")), task))
    out: dict[str, list[str]] = {}
    for recipe, items in per.items():
        items.sort(key=lambda t: t[0], reverse=True)
        tasks = [t for _, t in items]
        tasks.sort(key=lambda t: t != _ENTRY_TASK)  # stable: sysroot first, rest stay latest-first
        out[recipe] = tasks
    return out


def _execution_order(rows: list[Any]) -> dict[str, list[str]]:
    """Per recipe, its executed tasks in the order they started (earliest first)."""
    per: dict[str, list[tuple[float, str]]] = {}
    for row in rows:
        recipe, task = native_ledger.row_pn(row), str(row.get("task") or "")
        if recipe and task:
            per.setdefault(recipe, []).append((_num(row.get("started")), task))
    return {recipe: [t for _, t in sorted(items)] for recipe, items in per.items()}


def _add(mapping: dict[str, list[str]], reason: str, recipe: str) -> None:
    mapping.setdefault(reason, []).append(recipe)


def _run_helper(request: dict[str, Any], bitbake_lib: Path, timeout: float) -> tuple[dict[str, Any] | None, str]:
    """Run the helper once; return ``(response, "")`` or ``(None, error)``."""
    siggen = bitbake_lib / "bb" / "siggen.py"
    if not siggen.is_file():
        return None, f"bitbake library not found: {siggen} does not exist"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(bitbake_lib)
    argv = [sys.executable, str(Path(__file__).with_name("sigdiff_helper.py"))]
    try:
        # The request goes to the helper on stdin from an anonymous temp file, not
        # as ``input=``. It is JSON the helper parses, never a command, but
        # opengrep's subprocess-injection rule matches ``input=`` carrying a dynamic
        # value, and a file on stdin is equivalent for the helper. ``subprocess.run``
        # still kills the child when the timeout expires, which ``Popen`` would not.
        with tempfile.TemporaryFile("w+", encoding="utf-8") as stdin_file:
            json.dump(request, stdin_file)
            stdin_file.seek(0)
            proc = subprocess.run(
                argv, stdin=stdin_file, capture_output=True, text=True, timeout=timeout, env=env, check=False
            )
    except subprocess.TimeoutExpired:
        return None, f"signature comparison timed out after {timeout:g}s"
    except OSError as exc:
        return None, f"signature comparison could not start: {exc}"
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()
        tail = detail[-1] if detail else "no message"
        return None, f"signature comparison failed (exit {proc.returncode}): {tail}"
    try:
        response = json.loads(proc.stdout)
    except ValueError as exc:
        return None, f"signature comparison returned unreadable output: {exc}"
    if not isinstance(response, dict):
        return None, "signature comparison returned unreadable output: not a JSON object"
    return response, ""


def natives_report(  # noqa: PLR0913 - keyword-only signature fixed by the spec
    artifact: dict[str, Any],
    *,
    run_dir: Path,
    sstate_dir: Path,
    sstate_namespaces: list[Path],
    stamp_roots: list[Path],
    bitbake_lib: Path,
    timeout: float = 600.0,
    extra_allowed_roots: Sequence[Path] = (),
) -> NativesReport:
    """Attribute the run's native/cross rebuilds to signature changes."""
    rows = list(native_ledger.executed_native_tasks(artifact))
    restored = native_ledger.restored_native_count(artifact)
    if not rows:
        return NativesReport(restored=restored, message=NO_DATA_MESSAGE)

    entries = _entry_tasks(rows)
    recipes = sorted(entries)
    base = {"executed_tasks": len(rows), "restored": restored, "rebuilt_recipes": len(recipes)}

    manifest = _load_manifest(Path(run_dir) / native_ledger.MANIFEST_NAME)
    if manifest is None:
        return NativesReport(**base, error=ERR_NO_MANIFEST)

    buckets = _Buckets()
    window = correlation_window(artifact)
    start = window[0] if window else None

    # Step 3: current signature per recipe.
    current: dict[str, _Pending] = {}
    for recipe in recipes:
        chosen = next((t for t in entries[recipe] if (recipe, t) in manifest), None)
        if chosen is None:
            _add(buckets.not_recoverable, REASON_NO_CURRENT, recipe)
            continue
        hash_ = manifest[(recipe, chosen)]
        path = native_ledger.ledger_entry(sstate_dir, recipe, chosen, hash_) or native_ledger.stamps_entry(
            stamp_roots, recipe, chosen, hash_
        )
        if path is None:
            _add(buckets.not_recoverable, REASON_CURRENT_MISSING, recipe)
            continue
        current[recipe] = _Pending(recipe, chosen, hash_, path, path)

    # Step 4: previous signature strictly older than the run window.
    if start is None:
        for recipe in current:
            _add(buckets.no_previous, REASON_NO_WINDOW, recipe)
        current = {}
    previous: dict[str, tuple[str, Path]] = {}
    order = _execution_order(rows)
    for recipe in list(current):
        # Hash equivalence can keep do_populate_sysroot's signature stable while earlier
        # tasks changed, so look for the first executed task whose signature differs from
        # its ledger predecessor before settling for the entry task.
        for task in order.get(recipe, []):
            hash_ = manifest.get((recipe, task))
            if hash_ is None:
                continue
            earlier = native_ledger.earlier_ledger_signatures(sstate_dir, recipe, task, before=start)
            if not earlier or earlier[0][0] == hash_:
                continue
            path = native_ledger.ledger_entry(sstate_dir, recipe, task, hash_) or native_ledger.stamps_entry(
                stamp_roots, recipe, task, hash_
            )
            if path is not None:
                current[recipe] = _Pending(recipe, task, hash_, path, path)
                previous[recipe] = (earlier[0][0], earlier[0][1])
                break
        if recipe in previous:
            continue
        earlier = native_ledger.earlier_ledger_signatures(sstate_dir, recipe, current[recipe].task, before=start)
        if earlier:
            previous[recipe] = (earlier[0][0], earlier[0][1])
    lacking = {r: p.task for r, p in current.items() if r not in previous}
    scan_incomplete = False
    if lacking:
        scan = native_ledger.scan_sstate_siginfo(
            sstate_namespaces, lacking, before=start, deadline=time.monotonic() + _SCAN_BUDGET_SECONDS
        )
        scan_incomplete = not scan.complete
        for recipe, found in scan.found.items():
            if found:
                previous[recipe] = (found[0][0], found[0][1])

    to_compare: list[_Pending] = []
    for recipe, pend in current.items():
        prev = previous.get(recipe)
        if prev is None:
            _add(buckets.no_previous, REASON_SCAN_INCOMPLETE if scan_incomplete else REASON_NO_PREVIOUS, recipe)
        elif prev[0] == pend.hash_:
            buckets.unchanged.append(recipe)
        else:
            pend.old = prev[1]
            to_compare.append(pend)

    common = {
        **base,
        "no_previous": sum(len(v) for v in buckets.no_previous.values()),
        "no_previous_groups": _reason_groups(buckets.no_previous),
        "unchanged": len(buckets.unchanged),
    }

    # Step 5: one helper call for every comparison.
    response: dict[str, Any] = {"results": [], "errors": []}
    if to_compare:
        request = {
            "roots": {
                "stamps": [str(p) for p in stamp_roots],
                "ledger": str(native_ledger.ledger_root(sstate_dir)),
                "sstate": [str(p) for p in sstate_namespaces],
                "also_allowed": [str(p) for p in extra_allowed_roots],
            },
            "comparisons": [
                {"recipe": p.recipe, "task": p.task, "old": str(p.old), "new": str(p.new)} for p in to_compare
            ],
        }
        got, err = _run_helper(request, Path(bitbake_lib), timeout)
        if got is None:
            return NativesReport(**common, error=err)
        response = got

    # Step 6: classify and group.
    results = {
        (str(r.get("recipe")), str(r.get("task"))): r.get("lines")
        for r in response.get("results") or []
        if isinstance(r, dict)
    }
    errors = {
        (str(e.get("recipe")), str(e.get("task"))): str(e.get("reason") or "unknown error")
        for e in response.get("errors") or []
        if isinstance(e, dict)
    }
    cause_recipes: dict[tuple[str, str], list[tuple[str, str]]] = {}
    attributed = 0
    for pend in to_compare:
        key = (pend.recipe, pend.task)
        if key in errors:
            _add(buckets.not_recoverable, errors[key], pend.recipe)
            continue
        lines = results.get(key)
        if not isinstance(lines, list):
            _add(buckets.not_recoverable, REASON_NO_RESULT, pend.recipe)
            continue
        causes: list[Cause] = leaf_causes([str(x) for x in lines], f"{pend.recipe}:{pend.task}")
        unrecoverable = [c for c in causes if c.kind == "unrecoverable"]
        if unrecoverable:
            _add(buckets.not_recoverable, unrecoverable[0].subject or "unrecoverable", pend.recipe)
            continue
        if not causes:
            _add(buckets.not_recoverable, REASON_NO_DIFFERENCE, pend.recipe)
            continue
        attributed += 1
        first_task: dict[tuple[str, str], str] = {}
        for c in causes:
            first_task.setdefault((c.kind, c.subject), c.task.rpartition(":")[2])
        for group_key, cause_task in first_task.items():
            cause_recipes.setdefault(group_key, []).append((pend.recipe, cause_task))

    groups = tuple(
        CauseGroup(kind, subject, len(members), tuple(members[:_MAX_EXAMPLES]))
        for (kind, subject), members in sorted(cause_recipes.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    )
    return NativesReport(
        **common,
        attributed=attributed,
        not_recoverable=sum(len(v) for v in buckets.not_recoverable.values()),
        not_recoverable_groups=_reason_groups(buckets.not_recoverable),
        groups=groups,
    )
