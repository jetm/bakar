"""Tests for the command-tree cache plugin in ``tests/conftest.py``."""

from __future__ import annotations

import typer
import typer.testing
from typer.testing import CliRunner

from tests.conftest import ClickTreeCache


def _make_app(label: str) -> typer.Typer:
    app = typer.Typer()

    @app.command()
    def hello() -> None:
        typer.echo(label)

    @app.command()
    def other() -> None:
        typer.echo("other")

    return app


def _install(monkeypatch, cache: ClickTreeCache):
    """Install ``cache`` over a counting builder; return the list of built apps."""
    built: list[object] = []
    real = typer.main.get_command

    def spy(app):
        built.append(app)
        return real(app)

    monkeypatch.setattr(typer.testing, "_get_command", spy)
    cache.pytest_configure(None)  # type: ignore[arg-type]
    return built


def test_repeated_invocations_build_the_tree_once(monkeypatch) -> None:
    cache = ClickTreeCache()
    built = _install(monkeypatch, cache)
    app = _make_app("a")
    runner = CliRunner()
    for _ in range(3):
        result = runner.invoke(app, ["hello"])
        assert result.exit_code == 0
        assert "a" in result.output
    assert built == [app]


def test_second_app_gets_its_own_tree(monkeypatch) -> None:
    cache = ClickTreeCache()
    built = _install(monkeypatch, cache)
    first, second = _make_app("first"), _make_app("second")
    runner = CliRunner()
    assert "first" in runner.invoke(first, ["hello"]).output
    assert "second" in runner.invoke(second, ["hello"]).output
    assert "first" in runner.invoke(first, ["hello"]).output
    assert built == [first, second]


def test_cache_holds_a_reference_to_the_app(monkeypatch) -> None:
    cache = ClickTreeCache()
    _install(monkeypatch, cache)
    app = _make_app("a")
    CliRunner().invoke(app, ["hello"])
    assert cache._trees[id(app)][0] is app


def test_unconfigure_restores_builder_and_clears(monkeypatch) -> None:
    cache = ClickTreeCache()
    _install(monkeypatch, cache)
    installed = typer.testing._get_command
    original = cache._original
    app = _make_app("a")
    CliRunner().invoke(app, ["hello"])
    cache.pytest_unconfigure(None)  # type: ignore[arg-type]
    assert typer.testing._get_command is not installed
    assert typer.testing._get_command is original
    assert not cache._trees
