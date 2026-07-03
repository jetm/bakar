"""CLI behavior tests for the ``-w``/``--workspace`` chdir wiring.

Task 3.1 of ``bakar-stop-and-workspace-cwd``: the shared ``WorkspaceOption``
callback (``_workspace_callback`` -> ``_enter_workspace``) resolves ``-w`` to an
absolute path, validates it, and ``os.chdir``s into it BEFORE the command body
runs. These tests drive the real Typer ``CliRunner`` from a directory OUTSIDE a
synthetic workspace so a relative positional kas YAML only resolves when the
callback chdir ran first.

All heavy side effects (``build_stop.stop_build``, ``step_kas.run_build``,
doctor checks, ``report``'s run lookup) are stubbed so no real build, signal, or
container is touched. ``monkeypatch.chdir`` restores the prior cwd on teardown.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import bakar.commands as commands_pkg
import bakar.commands.build as build_cmd
import bakar.commands.report as report_module
import bakar.commands.stop as stop_cmd
from bakar.cli import app
from bakar.commands._helpers import _WORKSPACE_HELP
from bakar.report import ReportSummary

if TYPE_CHECKING:
    from typer.testing import CliRunner as _CliRunner

pytestmark = pytest.mark.unit

_GENERIC_YAML = "header:\n  version: 21\nmachine: qemux86-64\ndistro: nodistro\ntarget: core-image-minimal\n"


@pytest.fixture
def runner() -> _CliRunner:
    from typer.testing import CliRunner

    return CliRunner()


@pytest.fixture(autouse=True)
def _stub_doctor_checks() -> None:
    """Stub doctor's ``run_all`` so build stays host-independent (real checks BLOCK)."""
    from unittest.mock import patch

    with patch("bakar.commands._helpers.run_all", return_value=[]):
        yield


@pytest.fixture(autouse=True)
def _stub_user_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force a fixed UserConfig so the build callback does not read the real config."""
    import bakar.commands._app as _state
    from bakar.user_config import UserConfig

    monkeypatch.setattr(_state, "_load_user_config_safe", lambda: UserConfig(hashserv=False))


def _make_workspace(root: Path) -> Path:
    """A ``.bakar.toml``-marked workspace holding a generic kas YAML named ``my.yml``."""
    root.mkdir(parents=True, exist_ok=True)
    (root / ".bakar.toml").write_text("")
    (root / "my.yml").write_text(_GENERIC_YAML)
    return root


def _outside_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """chdir into a dir that is NOT the workspace and return it."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    return elsewhere


def _record_stop(monkeypatch: pytest.MonkeyPatch, *, result: bool = True) -> list[tuple[Path, bool]]:
    """Stub ``build_stop.stop_build`` to record its calls and return ``result``."""
    calls: list[tuple[Path, bool]] = []

    def _rec(bsp_root: Path, force: bool) -> bool:
        calls.append((bsp_root, force))
        return result

    monkeypatch.setattr(stop_cmd.build_stop, "stop_build", _rec)
    return calls


# ---------------------------------------------------------------------------
# Relative positional YAML resolves via the callback chdir (the core fix)
# ---------------------------------------------------------------------------


def test_stop_relative_yaml_resolves_via_workspace_chdir(
    runner: _CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``bakar stop -w <ws> my.yml`` from outside <ws> resolves the relative YAML."""
    ws = _make_workspace(tmp_path / "ws")
    _outside_cwd(tmp_path, monkeypatch)

    calls = _record_stop(monkeypatch)

    result = runner.invoke(app, ["stop", "--workspace", str(ws), "my.yml"])

    assert result.exit_code == 0, result.output
    assert "kas YAML not found" not in result.output
    assert len(calls) == 1


def test_build_relative_yaml_resolves_via_workspace_chdir(
    runner: _CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``bakar build -w <ws> my.yml`` from outside <ws> resolves the relative YAML."""
    ws = _make_workspace(tmp_path / "ws")
    _outside_cwd(tmp_path, monkeypatch)

    calls: list[object] = []
    monkeypatch.setattr(
        build_cmd.step_kas,
        "run_build",
        lambda ctx, *, extra_overlays=None, show_layers=False: calls.append(ctx) or 0,
    )

    result = runner.invoke(app, ["build", "--workspace", str(ws), "my.yml"])

    assert result.exit_code == 0, result.output
    assert "kas YAML not found" not in result.output
    assert len(calls) == 1


def test_relative_workspace_value_does_not_double_resolve(
    runner: _CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A relative ``-w ./ws my.yml`` resolves once; the callback returns an absolute
    path so ``_resolve_workspace`` cannot re-resolve ``./ws`` against the new cwd
    into ``ws/ws`` (guards the reported double-resolve bug)."""
    ws = _make_workspace(tmp_path / "ws")
    # cwd is the parent of ws, so the relative './ws' resolves to <tmp>/ws exactly once.
    monkeypatch.chdir(tmp_path)

    calls = _record_stop(monkeypatch)

    result = runner.invoke(app, ["stop", "--workspace", "./ws", "my.yml"])

    assert result.exit_code == 0, result.output
    assert "kas YAML not found" not in result.output
    assert len(calls) == 1
    bsp_root = calls[0][0]
    # The resolved bsp_root lives under <tmp>/ws, never under a doubled <tmp>/ws/ws.
    assert (ws / "ws") not in bsp_root.parents
    assert bsp_root == ws.resolve() or ws.resolve() in bsp_root.parents


# ---------------------------------------------------------------------------
# A no-YAML command still works under -w
# ---------------------------------------------------------------------------


def test_no_yaml_command_report_succeeds_under_workspace(
    runner: _CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``report -w <ws>`` (no positional YAML) still resolves and succeeds."""
    ws = tmp_path / "ws"
    (ws / "nxp").mkdir(parents=True)
    _outside_cwd(tmp_path, monkeypatch)

    run_dir = ws / "nxp" / "build" / "runs" / "20260527-100000"
    summary = ReportSummary(
        run_id="20260527-100000",
        status="success",
        duration_s=10.0,
        deploy_dir="/deploy",
        image_size=1,
        layers=[],
    )
    monkeypatch.setattr(report_module, "_find_run", lambda runs_dirs, run_id: (run_dir, "nxp"))
    monkeypatch.setattr(report_module, "assemble_report", lambda run_dir, cfg: summary)

    result = runner.invoke(app, ["report", "--workspace", str(ws)])

    assert result.exit_code == 0, result.output
    assert "20260527-100000" in result.output


# ---------------------------------------------------------------------------
# Invalid -w exits 2 naming --workspace, before any signal
# ---------------------------------------------------------------------------


def test_missing_workspace_dir_exits_2_and_sends_no_signal(
    runner: _CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``-w <missing-dir>`` exits 2 naming ``--workspace`` before dispatch."""
    _outside_cwd(tmp_path, monkeypatch)

    calls = _record_stop(monkeypatch)

    missing = tmp_path / "nope"
    result = runner.invoke(app, ["stop", "--workspace", str(missing), "my.yml"])

    assert result.exit_code == 2, result.output
    # Typer color mode splits "--workspace" with ANSI codes; assert the plain word.
    assert "workspace" in result.output
    assert calls == []


def test_file_workspace_exits_2_and_sends_no_signal(
    runner: _CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``-w <a-file>`` (regular file, not a dir) exits 2 naming ``--workspace``."""
    _outside_cwd(tmp_path, monkeypatch)

    a_file = tmp_path / "afile"
    a_file.write_text("")

    calls = _record_stop(monkeypatch)

    result = runner.invoke(app, ["stop", "--workspace", str(a_file), "my.yml"])

    assert result.exit_code == 2, result.output
    # Typer color mode splits "--workspace" with ANSI codes; assert the plain word.
    assert "workspace" in result.output
    assert calls == []


# ---------------------------------------------------------------------------
# Centralization: no inline --workspace option; every -w help is unified
# ---------------------------------------------------------------------------


def _workspace_params() -> list[tuple[list[str], object]]:
    """Walk the click command tree, returning (command-path, param) for every
    parameter named ``workspace``."""
    import typer.main

    root = typer.main.get_command(app)
    found: list[tuple[list[str], object]] = []

    def walk(cmd: object, path: list[str]) -> None:
        subcommands = getattr(cmd, "commands", None)
        if subcommands:
            for name, sub in subcommands.items():
                walk(sub, [*path, name])
            return
        params = getattr(cmd, "params", [])
        found.extend((path, p) for p in params if getattr(p, "name", None) == "workspace")

    walk(root, [])
    return found


def test_no_command_module_declares_inline_workspace_option() -> None:
    """Only ``_helpers.py`` (the shared option) and ``init.py`` (its own inline
    option for a not-yet-existing dir) may declare ``typer.Option("--workspace"``."""
    commands_dir = Path(commands_pkg.__file__).parent
    offenders = []
    for py in sorted(commands_dir.glob("*.py")):
        if py.name in {"_helpers.py", "init.py"}:
            continue
        if 'typer.Option("--workspace"' in py.read_text():
            offenders.append(py.name)
    assert offenders == [], f"inline --workspace option must be migrated to WorkspaceOption: {offenders}"


def test_every_migrated_workspace_help_equals_canonical_string() -> None:
    """Every ``-w`` option except ``bakar init``'s carries the unified help string."""
    params = _workspace_params()
    assert params, "expected at least one command with a workspace option"

    mismatches = []
    for path, param in params:
        if path and path[-1] == "init":
            continue
        if getattr(param, "help", None) != _WORKSPACE_HELP:
            mismatches.append((path, getattr(param, "help", None)))
    assert mismatches == [], f"non-canonical -w help text: {mismatches}"
