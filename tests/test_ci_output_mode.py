"""Tests for bakar's CI/plain output-mode resolution and plain rendering.

Resolver unit tests (task 1.1) live here; the integration behaviors referenced by the
threat model (task 8.1) are appended below the resolver block.
"""

from __future__ import annotations

from bakar.output_mode import OutputMode, resolve_output_mode


def test_piped_selects_plain() -> None:
    assert resolve_output_mode(None, isatty=False, ci_env=None) is OutputMode.PLAIN


def test_tty_no_ci_stays_rich() -> None:
    assert resolve_output_mode(None, isatty=True, ci_env=None) is OutputMode.RICH


def test_ci_env_selects_plain_on_tty() -> None:
    assert resolve_output_mode(None, isatty=True, ci_env="1") is OutputMode.PLAIN


def test_falsey_ci_env_selects_rich_on_tty() -> None:
    for ci in ("", "0", "false", "False"):
        assert resolve_output_mode(None, isatty=True, ci_env=ci) is OutputMode.RICH


def test_explicit_plain_override_wins_on_tty() -> None:
    assert resolve_output_mode(OutputMode.PLAIN, isatty=True, ci_env=None) is OutputMode.PLAIN


def test_explicit_rich_override_wins_under_ci() -> None:
    assert resolve_output_mode(OutputMode.RICH, isatty=False, ci_env="1") is OutputMode.RICH
