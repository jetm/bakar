"""Coverage for the bakar entry-point error interceptor (cli.main).

These tests invoke ``bakar.cli.main`` directly with ``sys.argv``
monkeypatched, so the actual entry-point path is exercised end-to-end
(no ``CliRunner``). Each test asserts both the return code and the
absence of Rich box-drawing characters in stderr, proving the
interceptor short-circuited before Typer's rich_utils panel formatter
could render.
"""

from __future__ import annotations

import sys

import pytest

from bakar.cli import main

pytestmark = pytest.mark.unit


def test_main_unknown_option_returns_exit_code_2_and_no_panel(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A typo'd option produces plain stderr - no Rich box-drawing characters."""
    monkeypatch.setattr(sys, "argv", ["bakar", "--no-such-option"])
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
        ["bakar", "hashserv", "status", "kas.yml", "second-extra"],
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
    """``bakar --help`` exits cleanly via the typer.Exit branch."""
    monkeypatch.setattr(sys, "argv", ["bakar", "--help"])
    rc = main()
    assert rc == 0


def test_main_returns_exit_code_from_typer_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``settings get`` on an unknown key raises typer.Exit(2); interceptor surfaces 2."""
    monkeypatch.setattr(sys, "argv", ["bakar", "settings", "get", "no.such.key"])
    rc = main()
    assert rc == 2


def test_main_buildtools_missing_returns_1_clean(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A BuildtoolsMissingError (host-mode inspection on a stock host) surfaces as a
    plain 'Error:' with rc 1, not a raw traceback."""
    import bakar.cli as cli_mod
    from bakar.steps.kas_build import BuildtoolsMissingError

    def _raise(**_kw: object) -> int:
        raise BuildtoolsMissingError("buildtools-extended toolchain not found; set BAKAR_BUILDTOOLS_DIR")

    monkeypatch.setattr(cli_mod, "app", _raise)
    monkeypatch.setattr(sys, "argv", ["bakar", "getvar", "FOO"])
    rc = main()
    captured = capsys.readouterr()
    assert rc == 1, captured.err
    assert "Error:" in captured.err
    assert "buildtools-extended" in captured.err
    assert "╭" not in captured.err
