"""Unit tests for bakar.fork_race_signatures.

Each canonical fork-race regex is exercised against a representative
positive fixture (a line we expect to fire) and a negative fixture (a
line that must NOT fire). The negative cases guard against over-broad
patterns that would mask real recipe bugs in stress-parse runs.

Positive fixtures are quoted from known symptom variants of this bug.
"""

from __future__ import annotations

import pytest

from bakar.fork_race_signatures import (
    FORK_RACE_SIGNATURES,
    FORK_RACE_SUGGESTION,
    scan,
)

pytestmark = pytest.mark.unit

POSITIVE_CASES: list[tuple[str, str]] = [
    (
        r"Parser-\d+\[\d+\]: segfault at",
        "kernel: Parser-12[34567]: segfault at 10 ip 00007f8e6c4d8b3a sp 00007fff",
    ),
    (
        r"parser thread killed/died",
        "ERROR: ParseError in None: Not all recipes parsed, parser thread killed/died?",
    ),
    (
        r"SystemError: Type does not define the tp_name field",
        "SystemError: Type does not define the tp_name field",
    ),
    (
        r"SystemError: bad argument to internal function",
        "SystemError: bad argument to internal function",
    ),
    (
        r"bb\.data_smart\.ExpansionError",
        "bb.data_smart.ExpansionError: Failure expanding 'OVERRIDES'",
    ),
    (
        r"AttributeError:.*has no attribute 'path'",
        "AttributeError: 'NoneType' object has no attribute 'path'",
    ),
    (
        r"Worker.*died unexpectedly",
        "Worker process 1234 died unexpectedly with signal 11",
    ),
    (
        r"TypeError: sequence item \d+: expected .* instance, .* found",
        "TypeError: sequence item 0: expected str instance, set found",
    ),
]

NEGATIVE_CASES: list[str] = [
    "NOTE: Running task 1234 of 9876 (virtual:native:/path/recipe.bb:do_compile)",
    "WARNING: recipe-foo: usage of unsigned literal",
    "SystemError",
    "AttributeError",
    "ParseStatus: 100% (12345/12345)",
    "INFO: Fetching git://git.kernel.org/...",
    "Currently 16 running tasks (123 of 9876)",
    "do_compile: succeeded",
    "TypeError: 'int' object is not subscriptable",
    "TypeError: sequence item",
]


def test_signatures_are_compiled_patterns() -> None:
    """All entries are pre-compiled re.Pattern objects with non-empty source."""
    assert FORK_RACE_SIGNATURES, "signature list must not be empty"
    for pattern in FORK_RACE_SIGNATURES:
        assert hasattr(pattern, "search"), f"{pattern!r} is not a compiled regex"
        assert pattern.pattern, "empty pattern slipped in"


@pytest.mark.parametrize(("expected_pattern", "line"), POSITIVE_CASES)
def test_positive_fixture_matches(expected_pattern: str, line: str) -> None:
    """Each documented symptom matches at least one signature, and the match
    comes from the regex we expect (cross-check against accidental aliasing)."""
    matchers = [p for p in FORK_RACE_SIGNATURES if p.search(line)]
    assert matchers, f"no signature matched {line!r}"
    assert any(p.pattern == expected_pattern for p in matchers), (
        f"{line!r} matched {[p.pattern for p in matchers]}, expected {expected_pattern!r}"
    )


@pytest.mark.parametrize("line", NEGATIVE_CASES)
def test_negative_fixture_does_not_match(line: str) -> None:
    """Benign log lines must not trip any signature (no false positives)."""
    matchers = [p.pattern for p in FORK_RACE_SIGNATURES if p.search(line)]
    assert not matchers, f"{line!r} unexpectedly matched {matchers}"


def test_scan_returns_pattern_and_line() -> None:
    """scan() pairs each hit with its matched pattern and the original line."""
    text = "\n".join(
        [
            "INFO: starting parse",
            "kernel: Parser-9[1234]: segfault at 10 ip ... sp ...",
            "INFO: parse complete",
        ]
    )
    hits = scan(text)
    assert len(hits) == 1
    pattern, line = hits[0]
    assert pattern.pattern == r"Parser-\d+\[\d+\]: segfault at"
    assert "Parser-9[1234]" in line


def test_scan_handles_multiple_matches_in_one_text() -> None:
    """Two distinct symptoms in the same log produce two entries."""
    text = "\n".join(
        [
            "ERROR: ParseError in None: Not all recipes parsed, parser thread killed/died?",
            "...",
            "SystemError: Type does not define the tp_name field",
        ]
    )
    hits = scan(text)
    assert len(hits) == 2
    patterns = {p.pattern for p, _ in hits}
    assert r"parser thread killed/died" in patterns
    assert r"SystemError: Type does not define the tp_name field" in patterns


def test_suggestion_string_describes_root_cause() -> None:
    """The shared suggestion message must describe the root cause and
    the manual workaround so triage and stress-parse stay consistent."""
    assert "fork" in FORK_RACE_SUGGESTION
    assert "bakar build" in FORK_RACE_SUGGESTION


def test_triage_suggestions_consume_shared_signatures() -> None:
    """triage.py must use the shared list rather than a private regex.

    Catches a future refactor that re-introduces a hardcoded inline
    pattern next to the suggestion string.
    """
    from bakar import triage

    fork_race_entries = [
        (pat, sug)
        for pat, sug in triage._SUGGESTIONS  # type: ignore[attr-defined]
        if sug == FORK_RACE_SUGGESTION
    ]
    assert len(fork_race_entries) == 1, (
        "triage._SUGGESTIONS must carry exactly one fork-race entry wired to FORK_RACE_SUGGESTION"
    )
    pattern, _ = fork_race_entries[0]
    for line in (line for _expected, line in POSITIVE_CASES):
        assert pattern.search(line), f"triage's combined regex missed {line!r}"
