"""Regression tripwire pinning ``bakar build``'s CLI flag surface.

Every option on ``bakar build`` is part of the user-facing contract, so an
internal signature refactor of ``commands/build.py`` must leave the surface
byte-identical. Flag NAMES alone are too weak a check: a reworded help string
or a changed default is just as user-visible as a rename, and neither shows up
in a name-only comparison. The fixture therefore records name, aliases,
required/flag-ness, default and help text per parameter, in declaration order.

The records are read off the click command that Typer builds - the same object
that renders ``--help`` - rather than scraped from the rendered help, because
rich wraps help text to the terminal width and a scrape would encode the
width of whoever regenerated the fixture. ``test_help_lists_every_flag`` still
renders the real ``--help`` at a pinned width so an option hidden from the
rendered output is caught too.

Regenerating after a LEGITIMATE flag addition::

    uv run python tests/test_build_flag_surface.py

Review the resulting fixture diff by hand before committing it - the whole
point of this file is that the diff is never silent.
"""

from __future__ import annotations

import re
from pathlib import Path

import click
import pytest
import typer
from typer.testing import CliRunner

from bakar.cli import app

pytestmark = pytest.mark.unit

FIXTURE = Path(__file__).parent / "fixtures" / "build_flag_surface.txt"

# Rich reads COLUMNS to size help output. Pinning it keeps the rendered help
# deterministic regardless of the developer's terminal.
_HELP_ENV = {"COLUMNS": "200", "TERM": "dumb", "NO_COLOR": "1"}

_FIELD_SEP = " | "


def _build_command() -> click.Command:
    """Return the click command backing ``bakar build``."""
    cli = typer.main.get_command(app)
    command = cli.get_command(click.Context(cli), "build")
    assert command is not None, "bakar build is no longer a registered command"
    return command


def _one_line(text: str | None) -> str:
    return " ".join(text.split()) if text else ""


def _record(param: click.Parameter) -> str:
    kind = "argument" if isinstance(param, click.Argument) else "option"
    return _FIELD_SEP.join(
        [
            ",".join(param.opts),
            kind,
            f"aliases={','.join(param.secondary_opts)}",
            f"required={param.required}",
            f"flag={bool(getattr(param, 'is_flag', False))}",
            f"default={param.default!r}",
            f"help={_one_line(getattr(param, 'help', None))}",
        ]
    )


def render_flag_surface() -> str:
    """Render the current flag surface in fixture form."""
    return "".join(f"{_record(p)}\n" for p in _build_command().params)


def _parse(text: str) -> dict[str, str]:
    return {line.split(_FIELD_SEP, 1)[0]: line for line in text.splitlines() if line.strip()}


def test_build_flag_surface_matches_fixture() -> None:
    expected_text = FIXTURE.read_text(encoding="utf-8")
    actual_text = render_flag_surface()
    expected, actual = _parse(expected_text), _parse(actual_text)

    problems: list[str] = [f"REMOVED {n}\n    was: {expected[n]}" for n in sorted(expected.keys() - actual.keys())]
    problems += [f"ADDED   {n}\n    now: {actual[n]}" for n in sorted(actual.keys() - expected.keys())]
    problems += [
        f"CHANGED {n}\n    was: {expected[n]}\n    now: {actual[n]}"
        for n in expected
        if n in actual and expected[n] != actual[n]
    ]

    expected_order = [line.split(_FIELD_SEP, 1)[0] for line in expected_text.splitlines() if line.strip()]
    actual_order = [line.split(_FIELD_SEP, 1)[0] for line in actual_text.splitlines() if line.strip()]
    if not problems and expected_order != actual_order:
        problems.append(f"REORDERED\n    was: {expected_order}\n    now: {actual_order}")

    assert not problems, (
        "bakar build's flag surface moved:\n"
        + "\n".join(problems)
        + "\n\nIf this is an intentional flag change, regenerate with:\n"
        f"    uv run python {Path(__file__).relative_to(Path(__file__).parents[1])}"
    )


def test_help_lists_every_flag() -> None:
    """The rendered help must still show every option the fixture records."""
    result = CliRunner().invoke(app, ["build", "--help"], env=_HELP_ENV)
    assert result.exit_code == 0, result.output

    # Tokenize rather than substring-match: `--keep-going` is a substring of
    # `--keep-going-XX`, so a rename would otherwise slip past this check.
    rendered = set(re.split(r"[^\w.-]+", result.output))
    missing = [
        opt
        for line in _parse(FIXTURE.read_text(encoding="utf-8")).values()
        if " | option | " in line
        for opt in line.split(_FIELD_SEP, 1)[0].split(",")
        if opt not in rendered
    ]
    assert not missing, f"options absent from `bakar build --help`: {missing}"


if __name__ == "__main__":
    FIXTURE.write_text(render_flag_surface(), encoding="utf-8")
    print(f"wrote {FIXTURE}")
