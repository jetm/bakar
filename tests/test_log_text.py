"""Tests for bakar.log_text."""

from __future__ import annotations

import pytest

from bakar.log_text import MAX_LINE_CHARS, clean_line, error_line


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
