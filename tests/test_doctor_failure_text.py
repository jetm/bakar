"""doctor_failure_text and blocking_failures pick and render blocking doctor failures."""

from __future__ import annotations

import itertools

import pytest

from bakar.diagnostics import (
    CheckResult,
    Severity,
    Status,
    any_blocking_failure,
    blocking_failures,
    doctor_failure_text,
)


def _r(name: str, message: str, severity: Severity = Severity.BLOCK, status: Status = Status.FAIL) -> CheckResult:
    return CheckResult(name, severity, status, message)


def test_single_blocking_failure() -> None:
    results = [_r("bitbake-locks", "bitbake.lock held by PID 4242 - another build is running")]
    assert doctor_failure_text(results) == "bitbake-locks: bitbake.lock held by PID 4242 - another build is running"


def test_two_blocking_failures_in_order_without_warning() -> None:
    results = [
        _r("first", "one"),
        _r("warned", "careful", severity=Severity.WARN),
        _r("second", "two"),
    ]
    assert doctor_failure_text(results) == "first: one; second: two"


def test_pass_and_skip_blocking_results_ignored() -> None:
    results = [_r("a", "ok", status=Status.PASS), _r("b", "skipped", status=Status.SKIP)]
    assert doctor_failure_text(results) is None


def test_warnings_only_and_empty_give_none() -> None:
    assert doctor_failure_text([_r("w", "x", severity=Severity.WARN)]) is None
    assert doctor_failure_text([]) is None


def test_blank_message_gives_bare_name() -> None:
    assert doctor_failure_text([_r("only-name", "")]) == "only-name"
    assert doctor_failure_text([_r("a", "  \n "), _r("b", "m")]) == "a; b: m"


def test_message_of_control_characters_only_gives_bare_name() -> None:
    assert doctor_failure_text([_r("chk", "\x00"), _r("other", "m")]) == "chk; other: m"
    assert doctor_failure_text([_r("chk", "\x1b[0m")]) == "chk"


def test_crashed_check_text_carried() -> None:
    text = doctor_failure_text([_r("x", "check crashed: ValueError('boom')")])
    assert text == "x: check crashed: ValueError('boom')"


def test_line_break_collapsed() -> None:
    text = doctor_failure_text([_r("x", "first\nsecond\r\nthird")])
    assert text == "x: first second third"


def test_colour_sequences_removed() -> None:
    text = doctor_failure_text([_r("x", "\x1b[31mred\x1b[0m text")])
    assert text is not None
    assert "\x1b" not in text
    assert "red" in text


def test_three_entries_bounded_to_200_with_all_names() -> None:
    def entry_result(name: str, total: int) -> CheckResult:
        return _r(name, "m" * (total - len(name) - 2))

    results = [entry_result("alpha", 58), entry_result("bravo", 248), entry_result("charlie", 162)]
    text = doctor_failure_text(results)
    assert text is not None
    assert len(text) <= 200
    for name in ("alpha", "bravo", "charlie"):
        assert name in text
    assert text.startswith(f"alpha: {'m' * 51}; ")


def test_long_message_bounded_and_marked() -> None:
    text = doctor_failure_text([_r("name", "x" * 300)])
    assert text is not None
    assert len(text) <= 200
    assert text.startswith("name: ")
    assert text.endswith("...")


@pytest.mark.parametrize(
    ("severity", "status"),
    list(itertools.product(Severity, Status)),
)
def test_any_blocking_failure_matches_definition(severity: Severity, status: Status) -> None:
    r = _r("c", "m", severity=severity, status=status)
    expected = severity is Severity.BLOCK and status is Status.FAIL
    assert any_blocking_failure([r]) is expected
    assert bool(blocking_failures([r])) is expected
