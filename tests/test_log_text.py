"""Tests for bakar.log_text."""

from __future__ import annotations

import pytest

from bakar.log_text import MAX_LINE_CHARS, clean_line, error_line, join_bounded, one_line


def _entry(name: str, length: int) -> str:
    """Return ``name:`` followed by space-free filler, ``length`` characters in all."""
    return name + ":" + "x" * (length - len(name) - 1)


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("ERROR: Nothing PROVIDES 'does-not-exist'", "ERROR: Nothing PROVIDES 'does-not-exist'"),
        (
            "ERROR:  OE-core's config sanity checker detected a potential misconfiguration.",
            "ERROR: OE-core's config sanity checker detected a potential misconfiguration.",
        ),
        (
            "2026-10-19 12:00:00 - ERROR    - Configuration file not found: x.yml",
            "ERROR: Configuration file not found: x.yml",
        ),
        ("\x1b[31mERROR: boom\x1b[0m", "ERROR: boom"),
        ("ERROR: a\rb", "ERROR: ab"),
    ],
)
def test_error_line_matches(line: str, expected: str) -> None:
    assert error_line(line) == expected


@pytest.mark.parametrize(
    "line",
    [
        "2026-10-19 12:00:00 - WARNING  - something odd",
        "2026-10-19 12:00:00 - INFO     - fine",
        "WARNING: ERROR: x",
        "xERROR: y",
        "    ERROR: continuation",
        "ERROR:",
        "ERROR: ",
    ],
)
def test_error_line_rejects(line: str) -> None:
    assert error_line(line) is None


def test_error_line_bounded() -> None:
    result = error_line("ERROR: " + "x" * 500)
    assert result is not None
    assert len(result) == MAX_LINE_CHARS == 200


def test_clean_line_keeps_last_carriage_return_segment() -> None:
    assert clean_line("Parsing 10%\rParsing 55%\rParsing 99%") == "Parsing 99%"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("a\nb", "a b"),
        ("a\r\n\tb  c", "a b c"),
        ("\x1b[31mred\x1b[0m text", "red text"),
        ("a \x00 b", "a b"),
        ("one\N{LINE SEPARATOR}two", "one two"),
        ("one \N{PARAGRAPH SEPARATOR} two", "one two"),
        (" \n\t\r ", ""),
    ],
)
def test_one_line_collapses(text: str, expected: str) -> None:
    assert one_line(text) == expected


def test_one_line_idempotent() -> None:
    sample = "  \x1b[1mERROR:\x1b[0m\tfirst\r\nsecond \x07 third\N{LINE SEPARATOR}fourth  "
    once = one_line(sample)
    assert once == "ERROR: first second third fourth"
    assert one_line(once) == once


def test_one_line_does_not_cut() -> None:
    text = "word " * 100
    assert len(one_line(text)) == 499


def test_join_bounded_fits() -> None:
    assert join_bounded(["a", "b", "c"]) == "a; b; c"
    assert join_bounded(["a", "b"], sep=" | ") == "a | b"


def test_join_bounded_empty() -> None:
    assert join_bounded([]) == ""


def test_join_bounded_shares_fairly() -> None:
    names = ["git-global-config", "kas-yaml-syntax", "host-preflight"]
    result = join_bounded([_entry(names[0], 58), _entry(names[1], 248), _entry(names[2], 162)])
    assert len(result) <= MAX_LINE_CHARS
    parts = result.split("; ")
    assert len(parts) == 3
    assert parts[0] == _entry(names[0], 58)
    assert parts[1].endswith("...")
    assert parts[2].endswith("...")
    for name, part in zip(names, parts, strict=True):
        assert part.startswith(name + ":")


def test_join_bounded_short_entry_leaves_room() -> None:
    first = _entry("bitbake-locks", 59)
    result = join_bounded([first, _entry("kas-yaml-syntax", 237)])
    assert len(result) == MAX_LINE_CHARS
    parts = result.split("; ")
    assert parts[0] == first
    assert parts[1].startswith("kas-yaml-syntax:")
    assert parts[1].endswith("...")


def test_join_bounded_single_long_entry() -> None:
    result = join_bounded([_entry("kas-yaml-syntax", 300)])
    assert len(result) == MAX_LINE_CHARS
    assert result.endswith("...")


def test_join_bounded_budget_below_entry_count() -> None:
    entries = ["abcdefghij"] * 10
    assert join_bounded(entries, limit=10) == "; ".join(entries)[:10]


def test_join_bounded_keeps_order() -> None:
    entries = ["z" * 80, "a", "m" * 150]
    parts = join_bounded(entries).split("; ")
    assert parts[1] == "a"
    assert parts[0].startswith("z")
    assert parts[2].startswith("m")


def test_join_bounded_no_space_before_marker() -> None:
    # A 48-character limit cuts the entry at 45 characters, right after a space.
    result = join_bounded(["word " * 80], limit=48)
    assert result == ("word " * 9).rstrip() + "..."
    assert " ..." not in result
