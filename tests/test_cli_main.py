"""Coverage for the bspctl entry-point error interceptor (cli.main).

These tests invoke ``bspctl.cli.main`` directly with ``sys.argv``
monkeypatched, so the actual entry-point path is exercised end-to-end
(no ``CliRunner``). Each test asserts both the return code and the
absence of Rich box-drawing characters in stderr, proving the
interceptor short-circuited before Typer's rich_utils panel formatter
could render.
"""

from __future__ import annotations

import sys

import pytest

from bspctl.cli import main

pytestmark = pytest.mark.unit


def test_main_unknown_option_returns_exit_code_2_and_no_panel(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A typo'd option produces plain stderr - no Rich box-drawing characters."""
    monkeypatch.setattr(sys, "argv", ["bspctl", "--no-such-option"])
    rc = main()
    captured = capsys.readouterr()
    assert rc == 2, captured.err
    assert "Error:" in captured.err
    # The box character `╭` is what Typer's Rich panel formatter emits. Its absence
    # proves the interceptor short-circuited before rich_utils could render the panel.
    assert "╭" not in captured.err
    assert "╰" not in captured.err


def test_main_unexpected_extra_argument_returns_2(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Two extra positionals after ``hashserv status kas.yml`` trip UsageError."""
    monkeypatch.setattr(
        sys,
        "argv",
        ["bspctl", "hashserv", "status", "kas.yml", "second-extra"],
    )
    rc = main()
    captured = capsys.readouterr()
    assert rc == 2, captured.err
    assert "Error:" in captured.err
    assert "╭" not in captured.err
    assert "╰" not in captured.err


def test_main_returns_0_on_normal_help(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``bspctl --help`` exits cleanly via the typer.Exit branch."""
    monkeypatch.setattr(sys, "argv", ["bspctl", "--help"])
    rc = main()
    assert rc == 0


def test_main_returns_exit_code_from_typer_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``settings get`` on an unknown key raises typer.Exit(2); interceptor surfaces 2."""
    monkeypatch.setattr(sys, "argv", ["bspctl", "settings", "get", "no.such.key"])
    rc = main()
    assert rc == 2
