"""Standalone signature-diff helper, run under a workspace's bitbake library.

Invoked as ``<python> <this file>`` with ``PYTHONPATH=<bitbake>/lib``. It must
not import anything from bakar: only bitbake's library is on the path.

Request (one JSON object on stdin)::

    {"roots": {"stamps": [dir], "ledger": dir, "sstate": [namespace dir],
                "also_allowed": [dir]},
     "comparisons": [{"recipe", "task", "old": path, "new": path}]}

``also_allowed`` is optional. Directories in it are permitted for opening (a
siginfo whose realpath lies inside one is accepted, same per-component lexical
then realpath containment as the other roots) but are never searched by the
hash lookup. It exists for bitbake's ``SSTATE_MIRRORS`` symlinks, which point
sstate siginfo into bakar's ``.native-seed/<release>`` directories.

Response (one JSON object on stdout)::

    {"results": [{"recipe", "task", "lines": [str]}],
     "errors": [{"recipe", "task", "reason"}]}

Exit codes: 0 when the protocol completed (per-comparison failures are in
``errors``); 2, with a message on stderr, when the request is unreadable or
``bb.siggen`` cannot be imported.
"""

from __future__ import annotations

import glob
import importlib
import json
import os
import re
import sys
from collections.abc import Callable
from pathlib import Path

_PN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+._-]*$")
_TASK_RE = re.compile(r"^do_[A-Za-z0-9_]+$")
_HASH_RE = re.compile(r"^[0-9a-f]{40,128}$")

CompareFn = Callable[..., list[str]]


def valid_pn(pn: str) -> bool:
    return bool(_PN_RE.match(pn)) and ".." not in pn


def valid_task(task: str) -> bool:
    return bool(_TASK_RE.match(task))


def valid_hash(value: str) -> bool:
    return bool(_HASH_RE.match(value))


def split_key(key: str) -> tuple[str, str] | None:
    """Return ``(pn, task)`` from ``pn:task`` or ``mc:<name>:pn:task``."""
    parts = key.split(":")
    if len(parts) < 2:
        return None
    return parts[-2], parts[-1]


def _resolved_roots(paths: list[str]) -> list[tuple[Path, Path]]:
    """Each root as (lexically normalized, realpath) pair."""
    return [(Path(os.path.normpath(p)), Path(os.path.realpath(p))) for p in paths if isinstance(p, str) and p]


def _within(path: str, roots: list[tuple[Path, Path]]) -> bool:
    """True when path lies inside a root both lexically and after realpath."""
    lex = Path(os.path.normpath(path))
    real = Path(os.path.realpath(path))
    return any(lex.is_relative_to(nl) and real.is_relative_to(rl) for nl, rl in roots)


class SigLookup:
    """Confined, hash-based sigdata lookup with a memoized recursion callback."""

    def __init__(self, roots: dict, compare: CompareFn) -> None:
        stamps = [r for r in roots.get("stamps") or [] if isinstance(r, str)]
        ledger = roots.get("ledger")
        sstate = [r for r in roots.get("sstate") or [] if isinstance(r, str)]
        self.stamps = stamps
        self.ledger = ledger if isinstance(ledger, str) and ledger else None
        self.sstate = sstate
        # Lookup-by-hash searches only the roots above. ``also_allowed`` only widens the
        # confinement check for symlink targets: bitbake symlinks SSTATE_MIRRORS siginfo
        # into SSTATE_DIR. Threat model: those seed dirs are bakar-owned under the sstate
        # root the user already trusts for object content.
        self._roots = _resolved_roots(stamps + ([self.ledger] if self.ledger else []) + sstate)
        self._also = _resolved_roots([r for r in roots.get("also_allowed") or [] if isinstance(r, str)])
        self._compare = compare
        self._memo: dict[tuple[str, str, str], list[str]] = {}
        self._active: set[tuple[str, str, str]] = set()

    def confined(self, path: str) -> bool:
        if _within(path, self._roots + self._also):
            return True
        # A symlink under a request root whose target lies inside an also_allowed dir.
        lex = Path(os.path.normpath(path))
        real = Path(os.path.realpath(path))
        return any(lex.is_relative_to(nl) for nl, _ in self._roots) and any(
            real.is_relative_to(rl) for _, rl in self._also
        )

    def find(self, pn: str, task: str, hash_: str) -> str | None:
        """Locate a sigdata file by hash; stamps, then ledger, then sstate."""
        if not (valid_pn(pn) and valid_task(task) and valid_hash(hash_)):
            return None
        patterns: list[str] = [
            os.path.join(glob.escape(root), "*", pn, f"*.{task}.sigdata.{hash_}") for root in self.stamps
        ]
        if self.ledger:
            patterns.append(os.path.join(glob.escape(self.ledger), pn, f"{task}.{hash_}.sigdata"))
        name = task[3:]
        for ns in self.sstate:
            patterns.append(  # noqa: PERF401
                os.path.join(
                    glob.escape(ns),
                    hash_[0:2],
                    hash_[2:4],
                    f"sstate:{pn}:*:{hash_}_{name}.tar.*.siginfo",
                )
            )
        for pattern in patterns:
            for hit in sorted(glob.glob(pattern)):
                if os.path.isfile(hit) and self.confined(hit):
                    return hit
        return None

    def recurse(self, key: str, hash1: str, hash2: str) -> list[str]:
        """The bitbake-diffsigs recursion callback, memoized by (key, h1, h2)."""
        memo_key = (key, hash1, hash2)
        cached = self._memo.get(memo_key)
        if cached is not None:
            return list(cached)
        if memo_key in self._active:
            return []
        self._active.add(memo_key)
        try:
            lines = self._recurse_uncached(key, hash1, hash2)
        finally:
            self._active.discard(memo_key)
        self._memo[memo_key] = lines
        return list(lines)

    def _recurse_uncached(self, key: str, hash1: str, hash2: str) -> list[str]:
        split = split_key(key)
        f1 = f2 = None
        if split is not None:
            f1 = self.find(split[0], split[1], hash1)
            f2 = self.find(split[0], split[1], hash2)
        if f1 is None and f2 is None:
            return [f"Unable to find matching sigdata for {key} with hashes {hash1} or {hash2}"]
        if f1 is None:
            return [f"Unable to find matching sigdata for {key} with hash {hash1}"]
        if f2 is None:
            return [f"Unable to find matching sigdata for {key} with hash {hash2}"]
        out: list[str] = []
        for change in self._compare(f1, f2, self.recurse, color=False):
            out.extend("    " + line for line in change.splitlines())
        return out


def run_request(request: object, compare: CompareFn) -> dict:
    """Execute a decoded request; raises ValueError when it is malformed."""
    if not isinstance(request, dict):
        raise ValueError("request must be a JSON object")  # noqa: TRY004
    roots = request.get("roots")
    comparisons = request.get("comparisons")
    if not isinstance(roots, dict) or not isinstance(comparisons, list):
        raise ValueError("request needs 'roots' and 'comparisons'")  # noqa: TRY004
    lookup = SigLookup(roots, compare)
    results: list[dict] = []
    errors: list[dict] = []
    for entry in comparisons:
        if not isinstance(entry, dict):
            errors.append({"recipe": "", "task": "", "reason": "comparison is not an object"})
            continue
        recipe = str(entry.get("recipe", ""))
        task = str(entry.get("task", ""))
        old, new = entry.get("old"), entry.get("new")
        if not isinstance(old, str) or not isinstance(new, str):
            errors.append({"recipe": recipe, "task": task, "reason": "old/new must be paths"})
            continue
        if not (lookup.confined(old) and lookup.confined(new)):
            errors.append({"recipe": recipe, "task": task, "reason": "path outside request roots"})
            continue
        try:
            lines = compare(old, new, lookup.recurse, color=False)
        except Exception as exc:  # noqa: BLE001
            errors.append({"recipe": recipe, "task": task, "reason": f"{type(exc).__name__}: {exc}"})
            continue
        results.append({"recipe": recipe, "task": task, "lines": [str(x) for x in lines]})
    return {"results": results, "errors": errors}


def main() -> int:
    try:
        request = json.load(sys.stdin)
    except (ValueError, OSError) as exc:
        print(f"sigdiff_helper: unreadable request: {exc}", file=sys.stderr)
        return 2
    # sys.path[0] is this file's directory, which holds bakar's own hashserv.py
    # and would shadow bitbake's hashserv package.
    here = os.path.dirname(os.path.realpath(__file__))
    sys.path[:] = [p for p in sys.path if os.path.realpath(p or os.getcwd()) != here]
    try:
        # bb.siggen exists only under the workspace bitbake's lib/, which the launcher puts on PYTHONPATH.
        siggen = importlib.import_module("bb.siggen")
    except Exception as exc:  # noqa: BLE001
        print(f"sigdiff_helper: cannot import bb.siggen: {exc}", file=sys.stderr)
        return 2
    try:
        response = run_request(request, siggen.compare_sigfiles)
    except ValueError as exc:
        print(f"sigdiff_helper: bad request: {exc}", file=sys.stderr)
        return 2
    json.dump(response, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
