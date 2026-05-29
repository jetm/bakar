"""Tests for the ``bakar clean`` command.

Each test sets up a tmp workspace with a ``.bakar.toml`` marker so
``_workspace_from_cwd`` finds the workspace; the ``nxp/`` subdir makes the
resolved ``cfg.bsp_root`` point at ``<workspace>/nxp``. Helpers that touch
the filesystem (``shutil.rmtree``, ``bakar.hashserv.stop``) are
monkeypatched so no real daemon is signaled and no real directories are
removed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from bakar.cli import app

if TYPE_CHECKING:
    from pathlib import Path

    from typer.testing import CliRunner as _CliRunner

pytestmark = pytest.mark.unit


@pytest.fixture
def runner() -> _CliRunner:
    from typer.testing import CliRunner

    return CliRunner()


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A tmp workspace with a ``.bakar.toml`` marker; chdir into it.

    Creates ``nxp/build/`` so ``_clean_build_dir`` follows its rmtree path
    (the helper is a no-op when the build dir is absent).
    """
    (tmp_path / ".bakar.toml").write_text("")
    (tmp_path / "nxp" / "build").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_clean_all_calls_hashserv_stop_before_wipe(
    runner: _CliRunner, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``clean --all`` must stop the hashserv daemon before any rmtree.

    Orphaning the daemon with a missing working directory is exactly the
    failure this hook prevents, so the test pins ORDER: ``stop`` first,
    ``rmtree`` afterwards. An unordered membership check would let a
    regression that stops the daemon AFTER the wipe pass silently.
    """
    import shutil

    from bakar import hashserv

    recorded: list[tuple[str, str]] = []

    monkeypatch.setattr(
        hashserv,
        "stop",
        lambda root: recorded.append(("stop", str(root))) or True,
    )
    monkeypatch.setattr(
        shutil,
        "rmtree",
        lambda path, *a, **kw: recorded.append(("rmtree", str(path))),
    )

    result = runner.invoke(app, ["clean", "--all", "--bsp", "nxp"])

    assert result.exit_code == 0, result.output
    assert recorded, "expected at least one recorded call"
    assert recorded[0][0] == "stop", f"first call must be stop, got {recorded!r}"
    assert any(entry[0] == "rmtree" for entry in recorded[1:]), (
        f"expected at least one rmtree after stop, got {recorded!r}"
    )
