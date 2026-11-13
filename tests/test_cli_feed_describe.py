"""The hidden ``bakar feed describe`` verb: one JSON document on stdout, errors on stderr."""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from bakar import feed_describe
from bakar.commands import _feed_describe as describe
from bakar.commands import app
from tests.test_feed_describe import RUN_ID, Host

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


@pytest.fixture
def cli() -> CliRunner:
    return CliRunner()


@pytest.fixture
def host(tmp_path: Path) -> Host:
    return Host(tmp_path)


@pytest.fixture
def real_describe(host: Host, monkeypatch: pytest.MonkeyPatch) -> Host:
    """Route the verb through the real ``describe_build`` against the fake host tree."""
    deps = dataclasses.replace(host.deps(), lock_probe=describe.probe_lock)

    def _describe(run_id: str, recipes: tuple[str, ...]) -> feed_describe.BuildDescription:
        return describe.describe_build(run_id, recipes, deps=deps)

    monkeypatch.setattr("bakar.commands.feed.describe_build", _describe)
    return host


def _listing(root: Path) -> dict[str, tuple[int, int, int]]:
    out = {}
    for p in sorted(root.rglob("*")):
        st = p.lstat()
        out[str(p)] = (st.st_mode, st.st_size, st.st_mtime_ns)
    return out


def test_success_prints_exactly_one_document_that_parses_back(cli: CliRunner, real_describe: Host) -> None:
    result = cli.invoke(app, ["feed", "describe", RUN_ID, "--recipe", "htop"])

    assert result.exit_code == 0, result.stderr
    assert result.stdout.endswith("\n")
    assert not result.stdout.endswith("\n\n")
    desc = feed_describe.parse(result.stdout)
    assert desc.run_id == RUN_ID
    assert desc.state == "succeeded"
    assert [e.recipe for e in desc.entries] == ["htop"]
    assert result.stdout == feed_describe.encode(desc) + "\n"


def test_recipe_option_is_repeatable_and_reaches_describe_build(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch, real_describe: Host
) -> None:
    seen: list[tuple[str, tuple[str, ...]]] = []
    inner = describe.describe_build
    deps = dataclasses.replace(real_describe.deps(), lock_probe=describe.probe_lock)
    real_describe.world.recipe("nano")

    def _spy(run_id: str, recipes: tuple[str, ...]) -> feed_describe.BuildDescription:
        seen.append((run_id, recipes))
        return inner(run_id, recipes, deps=deps)

    monkeypatch.setattr("bakar.commands.feed.describe_build", _spy)

    result = cli.invoke(app, ["feed", "describe", RUN_ID, "--recipe", "htop", "--recipe", "nano"])

    assert result.exit_code == 0, result.stderr
    assert seen == [(RUN_ID, ("htop", "nano"))]


@pytest.mark.parametrize(
    ("exc", "needle"),
    [
        (describe.DescribeError("no run X is registered on this host"), "no run X is registered on this host"),
        (ValueError("bad config"), "ValueError: bad config"),
        (OSError("disk gone"), "OSError: disk gone"),
    ],
)
def test_every_error_leaves_stdout_empty_and_exits_1(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch, exc: Exception, needle: str
) -> None:
    def _boom(run_id: str, recipes: tuple[str, ...]) -> feed_describe.BuildDescription:
        raise exc

    monkeypatch.setattr("bakar.commands.feed.describe_build", _boom)

    result = cli.invoke(app, ["feed", "describe", "X", "--recipe", "htop"])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert needle in result.stderr
    assert "Traceback" not in result.stderr
    assert len(result.stderr.strip().splitlines()) == 1


def test_describe_error_message_has_no_class_prefix(cli: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(run_id: str, recipes: tuple[str, ...]) -> feed_describe.BuildDescription:
        raise describe.DescribeError("plain message")

    monkeypatch.setattr("bakar.commands.feed.describe_build", _boom)

    result = cli.invoke(app, ["feed", "describe", "X", "--recipe", "htop"])

    assert result.stderr.strip() == "plain message"


def test_failed_run_prints_a_document_with_no_files_and_exits_0(cli: CliRunner, real_describe: Host) -> None:
    real_describe.write_run(terminal={"event": "run_end", "exit_code": 2})

    result = cli.invoke(app, ["feed", "describe", RUN_ID, "--recipe", "htop"])

    assert result.exit_code == 0, result.stderr
    desc = feed_describe.parse(result.stdout)
    assert desc.state == "failed"
    assert desc.exit_code == 2
    assert desc.files == ()
    assert desc.entries == ()


def test_missing_recipe_exits_2_with_usage_and_never_describes(cli: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []
    monkeypatch.setattr("bakar.commands.feed.describe_build", lambda *a, **k: calls.append(a))

    result = cli.invoke(app, ["feed", "describe", RUN_ID])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "--recipe" in result.stderr
    assert calls == []


def test_verb_is_hidden_from_the_visible_feed_help(cli: CliRunner) -> None:
    result = cli.invoke(app, ["feed", "--help"])

    assert result.exit_code == 0
    assert "describe" not in result.output
    assert "refresh" in result.output


def test_verb_is_still_reachable_by_name(cli: CliRunner) -> None:
    result = cli.invoke(app, ["feed", "describe", "--help"])

    assert result.exit_code == 0
    assert "--recipe" in result.output


def test_a_run_changes_nothing_under_the_host_tree_or_run_registry(
    cli: CliRunner, real_describe: Host, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "xdg-state"
    state.mkdir()
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    before = _listing(tmp_path)

    result = cli.invoke(app, ["feed", "describe", RUN_ID, "--recipe", "htop"])

    assert result.exit_code == 0, result.stderr
    assert _listing(tmp_path) == before
    assert not real_describe.lock_file.exists()
