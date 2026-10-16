"""Tests for the shared formatting helpers in ``bakar.fmt``."""

from __future__ import annotations

import unicodedata

import pytest

from bakar.fmt import fmt_duration, neutralize

pytestmark = pytest.mark.unit


def test_neutralize_escapes_rich_markup() -> None:
    assert neutralize("[bold]x[/]") == "\\[bold]x\\[/]"


def test_neutralize_strips_controls_and_format_chars() -> None:
    assert neutralize("a\x1b]0;t\x07b‮c​d\ud800e") == "a]0;tbcde"


def test_neutralize_bounds_length_keeping_both_ends() -> None:
    out = neutralize("A" * 600 + "B" * 600)
    assert len(out) == 1024
    assert out.startswith("A") and out.endswith("B") and "..." in out


def test_neutralize_strips_every_unicode_format_character() -> None:
    """Independent of the module's own regex: walk the code space with ``unicodedata``."""
    missed = [
        hex(code) for code in range(0x110000) if unicodedata.category(chr(code)) == "Cf" and neutralize(chr(code)) != ""
    ]
    assert missed == []


def test_neutralize_strips_every_c0_c1_control_and_surrogate() -> None:
    for code in [*range(0x20), *range(0x7F, 0xA0), *range(0xD800, 0xE000)]:
        assert neutralize(f"a{chr(code)}b") == "ab", hex(code)


def test_neutralize_accepts_non_str() -> None:
    assert neutralize(ValueError("x [y]")) == "x \\[y]"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("plain", "plain"),
        ("[bold]x", "\\[bold]x"),
        ("p[/]q", "p\\[/]q"),
        ("a\x00b" + chr(0x200B) + "c", "abc"),
        ("lib\x1b]0;pwned\x07z.so", "lib]0;pwnedz.so"),
        ("a" + chr(0x202E) + "b" + chr(0x2066) + "c", "abc"),
        ("x" + chr(0x2028) + "y" + chr(0x2029) + "z", "xyz"),
        ("a " + chr(0x2022) + " b", "a  b"),
        ("", ""),
    ],
)
def test_neutralize_table(value: str, expected: str) -> None:
    assert neutralize(value) == expected


@pytest.mark.parametrize("length", [1023, 1024])
def test_neutralize_leaves_values_at_or_under_the_limit_untouched(length: int) -> None:
    value = "z" * length
    assert neutralize(value) == value


def test_neutralize_elides_the_middle_just_over_the_limit() -> None:
    out = neutralize("z" * 1025)
    assert len(out) == 1024
    assert out.count("...") == 1
    assert out.startswith("z") and out.endswith("z")


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "0s"),
        (5, "5s"),
        (59, "59s"),
        (60, "1m00s"),
        (91, "1m31s"),
        (1471, "24m31s"),  # the 󰦗 global-timer value from a real build
        (3599, "59m59s"),
        (3600, "1h00m"),
        (3661, "1h01m"),
        (7325, "2h02m"),
    ],
)
def test_fmt_duration_boundaries(seconds: int, expected: str) -> None:
    assert fmt_duration(seconds) == expected


def test_fmt_duration_truncates_float_seconds() -> None:
    assert fmt_duration(42.9) == "42s"
    assert fmt_duration(1471.6) == "24m31s"
