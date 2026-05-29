"""Property-based tests for bakar.fork_race_signatures.scan."""

from __future__ import annotations

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from bakar.fork_race_signatures import FORK_RACE_SIGNATURES, scan


@pytest.mark.unit
@given(text=st.text())
@settings(suppress_health_check=[HealthCheck.too_slow])
def test_scan_never_raises(text: str) -> None:
    """scan accepts any string without raising."""
    scan(text)


@pytest.mark.unit
def test_scan_empty_returns_empty() -> None:
    """Empty input yields no matches."""
    assert scan("") == []


@pytest.mark.unit
@given(text=st.text())
@settings(suppress_health_check=[HealthCheck.too_slow])
def test_scan_lines_come_from_input(text: str) -> None:
    """Every matched line is one of the input's lines."""
    source_lines = text.splitlines()
    for _pattern, line in scan(text):
        assert line in source_lines


@pytest.mark.unit
@given(
    text=st.text(alphabet=st.characters(blacklist_categories=("Cs",))),
)
@settings(suppress_health_check=[HealthCheck.too_slow])
def test_scan_clean_text_returns_empty(text: str) -> None:
    """Text with no signature substrings yields no matches."""
    assume(not any(p.search(text) for p in FORK_RACE_SIGNATURES))
    assert scan(text) == []
