"""Tests for the shared formatting helpers in ``bakar.fmt``."""

from __future__ import annotations

import pytest

from bakar.fmt import fmt_duration

pytestmark = pytest.mark.unit


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
