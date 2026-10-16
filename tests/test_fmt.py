"""Tests for the shared formatting helpers in ``bakar.fmt``."""

from __future__ import annotations

import pytest

from bakar import elfscan
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


def test_neutralize_strips_exactly_what_elfscan_strips() -> None:
    from bakar import fmt

    for code in range(0x110000):
        ch = chr(code)
        assert fmt._CONTROL_RE.sub("", ch) == elfscan._CONTROL_RE.sub("", ch), hex(code)


def test_neutralize_accepts_non_str() -> None:
    assert neutralize(ValueError("x [y]")) == "x \\[y]"


@pytest.mark.parametrize("value", ["plain", "[bold]x", "a\x00b​c", "z" * 3000, "p[/]q"])
def test_neutralize_matches_elfscan_copy(value: str) -> None:
    assert neutralize(value) == elfscan._neutralized(value)


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
