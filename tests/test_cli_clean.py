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
from bakar.user_config import UserConfig

if TYPE_CHECKING:
    from pathlib import Path

    from typer.testing import CliRunner as _CliRunner

pytestmark = pytest.mark.unit


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A tmp workspace with a ``.bakar.toml`` marker; chdir into it.

    Creates ``nxp/build/`` so ``_clean_build_dir`` follows its rmtree path
    (the helper is a no-op when the build dir is absent).
    """
    (tmp_path / ".bakar.toml").write_text("")
    (tmp_path / "nxp" / "build").mkdir(parents=True)
    # No shared SSTATE_DIR: the hashserv daemon keys to bsp_root, so `clean --all`
    # stops it (the workspace-local path these tests pin). With a shared sstate,
    # clean leaves the shared daemon alone - covered in test_config.py.
    monkeypatch.delenv("SSTATE_DIR", raising=False)
    # The real ~/.config/bakar/config.toml sets sstate_dir for the cluster; load a
    # clean default so hashserv_state_key falls back to bsp_root, not the shared dir.
    monkeypatch.setattr("bakar.commands._app._load_user_config_safe", UserConfig)
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
    real_rmtree = shutil.rmtree

    def _recording_rmtree(path, *a, **kw):
        recorded.append(("rmtree", str(path)))
        real_rmtree(path, *a, **kw)

    monkeypatch.setattr(shutil, "rmtree", _recording_rmtree)

    result = runner.invoke(app, ["clean", "--all", "--bsp", "nxp"])

    assert result.exit_code == 0, result.output
    assert recorded, "expected at least one recorded call"
    assert recorded[0][0] == "stop", f"first call must be stop, got {recorded!r}"
    assert any(entry[0] == "rmtree" for entry in recorded[1:]), (
        f"expected at least one rmtree after stop, got {recorded!r}"
    )


def test_clean_all_does_not_stop_shared_daemon(
    runner: _CliRunner, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a shared SSTATE_DIR (state_key != bsp_root), ``clean --all`` must NOT
    stop the daemon: its DB lives outside the build dir and sibling workspaces
    depend on it. This pins the guard - inverting it would still pass the
    workspace-local test above, so the negative case needs its own assertion.
    """

    from bakar import hashserv

    # A shared sstate cache outside this workspace - the daemon is keyed to it.
    monkeypatch.setenv("SSTATE_DIR", str(workspace.parent / "shared-sstate"))

    stop_calls: list[str] = []
    monkeypatch.setattr(hashserv, "stop", lambda root: stop_calls.append(str(root)) or True)

    result = runner.invoke(app, ["clean", "--all", "--bsp", "nxp"])

    assert result.exit_code == 0, result.output
    assert stop_calls == [], f"shared daemon must not be stopped, got {stop_calls!r}"


def test_clean_refuses_before_stopping_hashserv_when_a_peer_holds_the_marker(
    runner: _CliRunner, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bakar import build_stop, hashserv

    marker = workspace / "nxp" / "build" / ".bakar-lock-marker"
    refusal = build_stop.LockRefusal(reason="peer-held", host="pc2", marker=marker)
    monkeypatch.setattr("bakar.build_stop.lock_mutation_guard", lambda c: refusal)
    stop_calls: list[str] = []
    monkeypatch.setattr(hashserv, "stop", lambda root: stop_calls.append(str(root)) or True)

    result = runner.invoke(app, ["clean", "--all", "--bsp", "nxp"])

    assert result.exit_code == 1, result.output
    assert stop_calls == [], f"hashserv must not be stopped on a refusal, got {stop_calls!r}"
    assert (workspace / "nxp" / "build").is_dir()
    assert "pc2" in result.output


def test_clean_all_proceeds_when_the_gate_has_no_refusal(
    runner: _CliRunner, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bakar import hashserv

    monkeypatch.setattr("bakar.build_stop.lock_mutation_guard", lambda c: None)
    monkeypatch.setattr("bakar.steps.kas_lock.local_build_holder", lambda c: None)
    stop_calls: list[str] = []
    monkeypatch.setattr(hashserv, "stop", lambda root: stop_calls.append(str(root)) or True)

    result = runner.invoke(app, ["clean", "--all", "--bsp", "nxp"])

    assert result.exit_code == 0, result.output
    assert len(stop_calls) == 1
    assert not (workspace / "nxp" / "build").exists()


def test_clean_refuses_when_a_local_build_is_live(
    runner: _CliRunner, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bakar import hashserv

    monkeypatch.setattr("bakar.build_stop.lock_mutation_guard", lambda c: None)
    monkeypatch.setattr("bakar.steps.kas_lock.local_build_holder", lambda c: 4242)
    stop_calls: list[str] = []
    monkeypatch.setattr(hashserv, "stop", lambda root: stop_calls.append(str(root)) or True)

    result = runner.invoke(app, ["clean", "--all", "--bsp", "nxp"])

    assert result.exit_code == 1, result.output
    assert "4242" in result.output
    assert stop_calls == []
    assert (workspace / "nxp" / "build").is_dir()


def test_clean_consults_the_gate_once_and_warns_once_when_unattributable(
    runner: _CliRunner, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bakar import build_stop, hashserv

    guard_calls: list[object] = []

    def _guard(cfg: object) -> build_stop.LockRefusal:
        guard_calls.append(cfg)
        return build_stop.LockRefusal(reason="unattributable", marker=workspace / "nxp" / "build" / "marker")

    monkeypatch.setattr("bakar.build_stop.lock_mutation_guard", _guard)
    monkeypatch.setattr("bakar.steps.kas_lock.local_build_holder", lambda c: None)
    monkeypatch.setattr(hashserv, "stop", lambda root: True)

    result = runner.invoke(app, ["clean", "--bsp", "nxp"])

    assert result.exit_code == 0, result.output
    # One gate run: the guard, then its single marker re-read. Not a second gate.
    assert len(guard_calls) == 2
    assert result.output.count("cannot attribute the build lock") == 1
    assert not (workspace / "nxp" / "build").exists()


@pytest.mark.parametrize(
    ("marker", "lock"),
    [
        (b"\xff\xfehost\n", b""),
        (None, b"\xff\xfe 123\n"),
        (None, b"99999999999\n"),
    ],
    ids=["marker-non-utf8", "lock-non-utf8", "lock-pid-beyond-int"],
)
def test_clean_warns_and_proceeds_on_undecodable_gate_files(
    runner: _CliRunner,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    marker: bytes | None,
    lock: bytes | None,
) -> None:
    from bakar import hashserv, mounts

    build = workspace / "nxp" / "build"
    if marker is not None:
        (build / ".bakar-lock-host").write_bytes(marker)
    if lock is not None:
        (build / "bitbake.lock").write_bytes(lock)
    monkeypatch.setattr(mounts, "is_path_on_nfs", lambda p: True)
    monkeypatch.setattr(hashserv, "stop", lambda root: True)

    result = runner.invoke(app, ["clean", "--bsp", "nxp"])

    assert result.exit_code == 0, result.output
    assert result.exception is None
    assert "cannot attribute the build lock" in result.output
    assert not build.exists()
