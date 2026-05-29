"""Canonical bitbake parser fork-race symptom regexes.

Single source of truth for both the post-mortem suggestion engine in
:mod:`bspctl.triage` and the empirical stress-test harness in
:mod:`bspctl.steps.stress_parse`. New variants observed in the
wild get added here once and both consumers pick them up.

Background: the upstream bug is a fork-in-multi-threaded-program race
in bitbake's parser. When the bitbake-server forks parser workers
while its own asyncio + idle threads are mid-CPython-mutation, the
child inherits torn state. The child can either crash outright
(SIGSEGV, ``parser thread killed/died``) or survive long enough to
trip CPython's internal type-slot validation (``SystemError: Type
does not define the tp_name field`` or sibling errors). Subtler
manifestations - corrupted ExpansionError reports, attribute lookups
on torn objects, generic worker-died traces - have been seen on
adjacent codepaths.


"""

from __future__ import annotations

import re

FORK_RACE_SIGNATURES: list[re.Pattern[str]] = [
    re.compile(r"Parser-\d+\[\d+\]: segfault at"),
    re.compile(r"parser thread killed/died"),
    re.compile(r"SystemError: Type does not define the tp_name field"),
    re.compile(r"SystemError: bad argument to internal function"),
    re.compile(r"bb\.data_smart\.ExpansionError"),
    re.compile(r"AttributeError:.*has no attribute 'path'"),
    re.compile(r"Worker.*died unexpectedly"),
    re.compile(r"TypeError: sequence item \d+: expected .* instance, .* found"),
]

FORK_RACE_SUGGESTION = (
    "bitbake parser worker corrupted by fork-in-multi-threaded-program race "
    "(CPython torn PyType state). Recurring upstream bug; not a recipe "
    "Manual workaround: re-run `bspctl build` - the next fork roll "
    "usually wins."
)


def scan(text: str) -> list[tuple[re.Pattern[str], str]]:
    """Return every (pattern, matched-line) pair that fired in ``text``.

    A line that matches multiple patterns yields one entry per pattern.
    The caller decides how to deduplicate (stress_parse keeps every
    pair so the summary preserves the full forensic record; triage
    deduplicates the suggestion string).
    """
    hits: list[tuple[re.Pattern[str], str]] = []
    for line in text.splitlines():
        for pattern in FORK_RACE_SIGNATURES:
            if pattern.search(line):
                hits.append((pattern, line))
    return hits
