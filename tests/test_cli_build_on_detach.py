"""``bakar build --on HOST --detach``: the flag reaches the dispatch, never a local unit."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from bakar.cli import app
from bakar.commands import build as build_cmd
from bakar.steps import avocado_dispatch as ad
from bakar.steps import remote_dispatch as rd
from tests.test_avocado_dispatch import HOST, Recorder, checkout, rec  # noqa: F401

if TYPE_CHECKING:
    from typer.testing import CliRunner as _CliRunner

pytestmark = pytest.mark.unit

ON = "pc2"


@pytest.fixture(autouse=True)
def _stub_doctor_checks():
    with patch("bakar.commands._helpers.run_all", return_value=[]):
        yield


@pytest.fixture
def no_local_unit(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    calls: list[Any] = []

    def boom(*args: Any, **kwargs: Any) -> None:
        calls.append((args, kwargs))
        raise AssertionError("--on must never start a local detach unit")

    monkeypatch.setattr(build_cmd._detach_cmd, "detach_and_exit", boom)
    return calls


def _record(monkeypatch: pytest.MonkeyPatch, module: Any, name: str) -> list[tuple[tuple, dict]]:
    calls: list[tuple[tuple, dict]] = []

    def fake(*args: Any, **kwargs: Any) -> int:
        calls.append((args, kwargs))
        return 0

    monkeypatch.setattr(module, name, fake)
    return calls


def _meta_avocado(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, lay: Any) -> list[tuple[tuple, dict]]:
    from bakar.steps import kas_pins
    from bakar.user_config import UserConfig

    cfg = UserConfig(workspaces_template=str(tmp_path / "absent-{release}"))
    monkeypatch.setattr("bakar.commands._app.load_user_config", lambda *a, **k: cfg)
    monkeypatch.setattr(kas_pins, "probe_pins", lambda _yaml, _checkout: ("wrynose", {}))
    monkeypatch.chdir(lay.worktree)
    return _record(monkeypatch, ad, "dispatch_avocado_build")


def _invoke(runner: _CliRunner, monkeypatch: pytest.MonkeyPatch, *args: str):
    argv = ["build", *args, "--on", ON, "--yes"]
    monkeypatch.setattr(sys, "argv", ["bakar", *argv])
    return runner.invoke(app, argv)


def test_detach_is_not_forwarded_to_the_remote_from_meta_avocado(
    rec: Recorder,  # noqa: F811
    checkout: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    specs: list[rd.RemoteScriptSpec] = []
    real_build = rd.build_remote_script

    def spy(spec: rd.RemoteScriptSpec) -> str:
        specs.append(spec)
        return real_build(spec)

    runs: list[tuple[tuple, dict]] = []

    def fake_run(*args: Any, **kwargs: Any) -> int:
        runs.append((args, kwargs))
        return 0

    monkeypatch.setattr(rd, "build_remote_script", spy)
    monkeypatch.setattr(rd, "run_remote_build", fake_run)
    hints = lambda h, r, host: [h]  # noqa: E731
    kas = str(checkout / "kas" / "machine" / "m.yml")
    flags = rd.DispatchFlags(sccache_dist=False, assume_yes=True, detach=True, hint_lines=hints)
    rc = ad.dispatch_avocado_build(
        HOST,
        ad.AvocadoDispatchRequest(
            workspace=Path("/srv/ws/scarthgap"),
            checkout=checkout,
            worktree=None,
            kas_yaml=checkout / "kas" / "machine" / "m.yml",
            kas_arg=kas,
            local_args=["build", kas, "--detach", "--on", HOST, "--yes"],
            release="scarthgap",
            worktree_root=None,
            build_dir_template=None,
            cap_mb=64,
            invoking_cwd=checkout,
            pinned_repos={},
        ),
        flags,
    )

    assert rc == 0
    assert len(specs) == 1
    assert "--detach" not in specs[0].remote_argv
    assert len(runs) == 1
    assert runs[0][1]["detach"] is True
    assert runs[0][1]["hint_lines"] is hints


def test_meta_avocado_dispatch_carries_detach_and_hints(
    runner: _CliRunner,
    meta_avocado_worktree_layout: Any,
    no_local_unit: list[Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    avocado = _meta_avocado(monkeypatch, tmp_path, meta_avocado_worktree_layout)

    result = _invoke(runner, monkeypatch, "kas/machine/m.yml", "--detach")

    assert result.exit_code == 0, result.output
    flags = avocado[0][0][2]
    assert flags.detach is True
    hints = list(flags.hint_lines("bakar-dispatch-x", "20260101-000000-abcdef", ON))
    assert hints[0].startswith("hint: wait: `bakar wait bakar-dispatch-x --timeout 540 --on pc2`")
    assert "background" in hints[0]
    assert "124" in hints[0]
    assert hints[1:] == [
        "hint: status: `bakar status bakar-dispatch-x --json --on pc2`",
        "hint: stop: `bakar stop --on pc2`",
    ]
    assert no_local_unit == []


def test_meta_avocado_on_alone_passes_detach_false(
    runner: _CliRunner,
    meta_avocado_worktree_layout: Any,
    no_local_unit: list[Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    avocado = _meta_avocado(monkeypatch, tmp_path, meta_avocado_worktree_layout)

    result = _invoke(runner, monkeypatch, "kas/machine/m.yml")

    assert result.exit_code == 0, result.output
    assert avocado[0][0][2].detach is False
    assert no_local_unit == []


@pytest.mark.parametrize(("extra", "expected"), [(["--detach"], True), ([], False)])
def test_generic_dispatch_flags_carry_detach(  # noqa: PLR0913
    runner: _CliRunner,
    no_local_unit: list[Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    extra: list[str],
    expected: bool,
) -> None:
    (tmp_path / ".bakar.toml").write_text("")
    (tmp_path / "nxp").mkdir()
    monkeypatch.chdir(tmp_path)
    yml = tmp_path / "my.yml"
    yml.write_text("header:\n  version: 14\nmachine: qemux86-64\n")
    mirror = _record(monkeypatch, rd, "dispatch_remote_build")

    result = _invoke(runner, monkeypatch, str(yml), *extra)

    assert result.exit_code == 0, result.output
    flags = mirror[0][0][2]
    assert flags.detach is expected
    wait_line = next(iter(flags.hint_lines("u", None, ON)))
    assert wait_line.startswith("hint: wait: `bakar wait u --timeout 540 --on pc2`")
    assert "background" in wait_line
    assert "124" in wait_line
    assert no_local_unit == []


@pytest.mark.parametrize(
    ("claudecode", "extra", "nudged"),
    [("1", [], True), ("1", ["--detach"], False), (None, [], False)],
)
def test_foreground_on_build_gets_the_nudge_only_in_agent_mode_without_detach(  # noqa: PLR0913
    runner: _CliRunner,
    no_local_unit: list[Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    claudecode: str | None,
    extra: list[str],
    nudged: bool,
) -> None:
    if claudecode is None:
        monkeypatch.delenv("CLAUDECODE", raising=False)
    else:
        monkeypatch.setenv("CLAUDECODE", claudecode)
    (tmp_path / ".bakar.toml").write_text("")
    (tmp_path / "nxp").mkdir()
    monkeypatch.chdir(tmp_path)
    yml = tmp_path / "my.yml"
    yml.write_text("header:\n  version: 14\nmachine: qemux86-64\n")
    _record(monkeypatch, rd, "dispatch_remote_build")
    emitted: list[list[str]] = []
    monkeypatch.setattr(build_cmd._hints, "emit", lambda lines: emitted.append(list(lines)))

    result = _invoke(runner, monkeypatch, str(yml), *extra)

    assert result.exit_code == 0, result.output
    nudge = [line for lines in emitted for line in lines if "run with --detach" in line]
    assert len(nudge) == (1 if nudged else 0)
    assert no_local_unit == []


def test_on_dry_run_gets_no_nudge(
    runner: _CliRunner, no_local_unit: list[Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.chdir(tmp_path)
    emitted: list[list[str]] = []
    monkeypatch.setattr(build_cmd._hints, "emit", lambda lines: emitted.append(list(lines)))
    argv = ["build", "my.yml", "--dry-run", "--on", ON, "--yes"]
    monkeypatch.setattr(sys, "argv", ["bakar", *argv])

    result = runner.invoke(app, argv)

    assert result.exit_code == 2
    assert emitted == []
