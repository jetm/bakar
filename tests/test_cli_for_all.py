"""Tests for the ``bakar for-all`` command.

Drives the command through the Typer ``CliRunner`` with ``discover_source_repos``
and ``subprocess.run`` monkeypatched in ``bakar.commands.for_all`` so no real
git checkout or shell invocation happens. The ``--workspace`` override plus an
``nxp/`` subdir lets workspace resolution succeed without a real BSP tree.

Follows the CliRunner + monkeypatch mock style in ``tests/test_cli_user_config.py``.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

import bakar.commands.for_all as for_all_module
from bakar.cli import app

if TYPE_CHECKING:
    from pathlib import Path

    from typer.testing import CliRunner as _CliRunner

pytestmark = pytest.mark.unit


@pytest.fixture
def nxp_workspace(tmp_path: Path) -> Path:
    """A workspace with an ``nxp/`` subdir so workspace detection picks nxp."""
    (tmp_path / "nxp").mkdir()
    return tmp_path


def _make_run_mock(repo_returncodes: dict[str, int], calls: list[dict]):
    """Build a ``subprocess.run`` replacement that records command invocations.

    ``_git_head`` calls ``subprocess.run`` with a ``["git", ...]`` list and
    ``shell`` unset; the command body calls it with the shell command string and
    ``shell=True``. The mock distinguishes the two by the ``shell`` kwarg: git
    HEAD lookups return a canned hash, command invocations record their kwargs
    into ``calls`` and return the per-repo rc keyed on ``BAKAR_REPO_NAME``.
    """

    def _run(cmd, *args, **kwargs):
        if not kwargs.get("shell"):
            # _git_head probe.
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="deadbeef\n", stderr="")
        env = kwargs.get("env") or {}
        calls.append({"cmd": cmd, "cwd": kwargs.get("cwd"), "env": env})
        rc = repo_returncodes.get(env.get("BAKAR_REPO_NAME", ""), 0)
        return subprocess.CompletedProcess(cmd, returncode=rc, stdout="", stderr="")

    return _run


def _patch_repos(monkeypatch: pytest.MonkeyPatch, repos):
    monkeypatch.setattr(for_all_module, "discover_source_repos", lambda cfg: repos)


@pytest.mark.unit
def test_runs_once_per_repo(runner: _CliRunner, nxp_workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The command invokes the shell once per discovered repo, in each repo's dir."""
    repos = [
        ("poky", nxp_workspace / "sources" / "poky"),
        ("meta-imx", nxp_workspace / "sources" / "meta-imx"),
        ("meta-freescale", nxp_workspace / "sources" / "meta-freescale"),
    ]
    _patch_repos(monkeypatch, repos)
    calls: list[dict] = []
    monkeypatch.setattr(subprocess, "run", _make_run_mock({}, calls))

    result = runner.invoke(app, ["for-all", "git status", "--workspace", str(nxp_workspace)])

    assert result.exit_code == 0, result.output
    assert len(calls) == 3
    assert all(c["cmd"] == "git status" for c in calls)
    assert [c["cwd"] for c in calls] == [path for _name, path in repos]


@pytest.mark.unit
def test_all_succeed_exits_zero(runner: _CliRunner, nxp_workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When every invocation returns zero, the aggregate exit is zero."""
    repos = [
        ("poky", nxp_workspace / "sources" / "poky"),
        ("meta-imx", nxp_workspace / "sources" / "meta-imx"),
    ]
    _patch_repos(monkeypatch, repos)
    calls: list[dict] = []
    monkeypatch.setattr(subprocess, "run", _make_run_mock({}, calls))

    result = runner.invoke(app, ["for-all", "true", "--workspace", str(nxp_workspace)])

    assert result.exit_code == 0, result.output
    assert len(calls) == 2


@pytest.mark.unit
def test_one_failing_repo_exits_nonzero_and_visits_all(
    runner: _CliRunner, nxp_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One non-zero invocation yields a non-zero aggregate while every repo is still visited."""
    repos = [
        ("poky", nxp_workspace / "sources" / "poky"),
        ("meta-imx", nxp_workspace / "sources" / "meta-imx"),
        ("meta-freescale", nxp_workspace / "sources" / "meta-freescale"),
    ]
    _patch_repos(monkeypatch, repos)
    calls: list[dict] = []
    # meta-imx fails; the run must still visit all three.
    monkeypatch.setattr(subprocess, "run", _make_run_mock({"meta-imx": 1}, calls))

    result = runner.invoke(app, ["for-all", "make", "--workspace", str(nxp_workspace)])

    assert result.exit_code != 0, result.output
    assert len(calls) == 3


@pytest.mark.unit
def test_per_repo_env_vars_reach_subprocess(
    runner: _CliRunner, nxp_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BAKAR_REPO_NAME/PATH/COMMIT are set per repo in the subprocess env."""
    repos = [
        ("poky", nxp_workspace / "sources" / "poky"),
        ("meta-imx", nxp_workspace / "sources" / "meta-imx"),
    ]
    _patch_repos(monkeypatch, repos)
    calls: list[dict] = []
    monkeypatch.setattr(subprocess, "run", _make_run_mock({}, calls))

    result = runner.invoke(app, ["for-all", "env", "--workspace", str(nxp_workspace)])

    assert result.exit_code == 0, result.output
    assert len(calls) == 2
    for (name, path), call in zip(repos, calls, strict=True):
        env = call["env"]
        assert env["BAKAR_REPO_NAME"] == name
        assert env["BAKAR_REPO_PATH"] == str(path)
        # _git_head is mocked to return "deadbeef".
        assert env["BAKAR_REPO_COMMIT"] == "deadbeef"


@pytest.mark.unit
def test_no_repos_exits_nonzero_with_guidance(
    runner: _CliRunner, nxp_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No discovered repos: exit non-zero, run the command zero times, print guidance."""
    _patch_repos(monkeypatch, [])
    calls: list[dict] = []
    monkeypatch.setattr(subprocess, "run", _make_run_mock({}, calls))

    result = runner.invoke(app, ["for-all", "git status", "--workspace", str(nxp_workspace)])

    assert result.exit_code != 0, result.output
    assert len(calls) == 0
    assert "no source repos found" in result.output
