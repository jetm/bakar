"""Canonical kas/bitbake failure-phase signature regexes.

A kas run walks three phases in order - it checks out the repos named
by the manifest, parses the resulting metadata, then runs build tasks.
A failure in the first phase reads nothing like a failure in the third,
but the caller only ever sees one blob of output, so a checkout failure
routinely gets mistaken for a recipe bug.

Background: the phase cannot be recovered from the process status.
``kas/kas.py:244-254`` collapses every ``KasUserError`` - including the
whole ``RepoRefError`` family raised across ``kas/repos.py`` during
checkout - to exit 2, while ``CommandExecError(forward=True)`` forwards
whatever inner code bitbake produced. The same status therefore covers
unrelated phases, and different statuses cover the same phase. Only the
text carries the answer, so :func:`classify` takes text and nothing
else.

Attribution is opt-in, never a fallback: text matching no signature
classifies as ``undetermined``. Guessing a phase would reproduce the
original defect - a caller sent hunting through the wrong subsystem -
with the added authority of a label.
"""

from __future__ import annotations

import re

CHECKOUT_SIGNATURES: list[re.Pattern[str]] = [
    # kas/repos.py:547-550 - the pinned commit is not on the pinned branch.
    re.compile(r'Branch "[^"]*" in\s+repository "[^"]*" does not contain\s+commit "[^"]*"'),
    # kas/repos.py:520, 537 - the pinned tag/branch does not resolve at all.
    re.compile(r'(?:Tag|Branch) "[^"]*" cannot be found\s+in repository "[^"]*"'),
    # kas/repos.py:527 - tag and commit pins disagree.
    re.compile(r'Provided tag "[^"]*" \([^)]*\) does not match\s+provided commit "[^"]*"'),
    # kas/repos.py:511 - mutually exclusive tag and branch pins.
    re.compile(r'Both tag "[^"]*" and branch "[^"]*"\s+cannot be specified for repository'),
    # kas/repos.py:280 - an unpinned remote repository.
    re.compile(r"No commit, tag or branch specified for\s+repository"),
]

PARSE_SIGNATURES: list[re.Pattern[str]] = [
    re.compile(r"ERROR: ParseError"),
    re.compile(r"ERROR: ExpansionError"),
    re.compile(r"ERROR: Unable to parse"),
    re.compile(r"ERROR: Nothing (?:PROVIDES|RPROVIDES)"),
    re.compile(r"ERROR: No recipes available for"),
]

BUILD_SIGNATURES: list[re.Pattern[str]] = [
    re.compile(r"ERROR: Task .* failed with exit code"),
    re.compile(r"ERROR: Logfile of failure stored in:"),
]

# Checked in kas's own execution order: a checkout failure means parse and
# build never ran, so the earliest matching phase is the failing one.
_PHASES: list[tuple[str, list[re.Pattern[str]]]] = [
    ("checkout", CHECKOUT_SIGNATURES),
    ("parse", PARSE_SIGNATURES),
    ("build", BUILD_SIGNATURES),
]


def classify(text: str) -> str:
    """Return the kas phase ``text`` failed in.

    One of ``checkout``, ``parse``, ``build``, or ``undetermined`` when
    no signature fires. Never infers a phase from anything but the text,
    and never falls back to a phase when unsure.

    Matching runs over the whole blob rather than line by line so a
    message already wrapped by a downstream renderer still matches.
    """
    for phase, signatures in _PHASES:
        if any(pattern.search(text) for pattern in signatures):
            return phase
    return "undetermined"
