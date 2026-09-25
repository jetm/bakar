"""CLI refusal tests for ``bakar hashserv start`` / ``bakar prserv start``.

Covers the ``daemon-state-filesystem-guard`` CLI requirement: when the
workspace's daemon state directory sits on a network filesystem (or one
``mounts.is_path_on_nfs`` cannot classify), ``ensure_running`` raises
``NetworkStateDirError`` (see ``tests/test_daemon_state_guard.py`` for the
underlying unit coverage). The CLI ``start`` commands must catch that error,
print it in red, exit 1, and must never create a PID file under the refused
state directory.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from bakar import mounts
from bakar.cli import app
from bakar.user_config import UserConfig

if TYPE_CHECKING:
    from pathlib import Path

    from typer.testing import CliRunner as _CliRunner

pytestmark = pytest.mark.unit


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A tmp workspace with a ``.bakar.toml`` marker; chdir into it.

    Mirrors ``tests/test_cli_hashserv.py``'s ``workspace`` fixture: an
    ``nxp/`` subdirectory exists so ``cfg.bsp_root`` resolves to
    ``<workspace>/nxp``, and no shared ``SSTATE_DIR``/user config is in
    play so the daemon state key falls back to ``bsp_root``.
    """
    (tmp_path / ".bakar.toml").write_text("")
    (tmp_path / "nxp").mkdir()
    monkeypatch.delenv("SSTATE_DIR", raising=False)
    monkeypatch.setattr("bakar.commands._app._load_user_config_safe", UserConfig)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_hashserv_start_refuses_nfs_state_dir(
    runner: _CliRunner, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``hashserv start`` exits 1, names ``bb_hashserve``, and creates no PID file."""
    monkeypatch.setattr(mounts, "is_path_on_nfs", lambda _p: True)

    result = runner.invoke(app, ["hashserv", "start"])

    assert result.exit_code == 1, result.output
    assert "bb_hashserve" in result.output
    assert not (workspace / "nxp" / ".bakar" / "hashserv.pid").exists()


def test_prserv_start_refuses_nfs_state_dir(
    runner: _CliRunner, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``prserv start`` exits 1, names ``prserv_host``, and creates no PID file."""
    monkeypatch.setattr(mounts, "is_path_on_nfs", lambda _p: True)

    result = runner.invoke(app, ["prserv", "start"])

    assert result.exit_code == 1, result.output
    assert "prserv_host" in result.output
    assert not (workspace / "nxp" / ".bakar" / "prserv.pid").exists()
