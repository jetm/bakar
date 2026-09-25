"""Unit tests for the daemon-state-filesystem-guard.

Covers ``hashserv.network_state_reason``/``daemon_state_refusal`` and the
hard-stop wired into ``hashserv.ensure_running`` / ``prserv.ensure_running``:
a per-workspace daemon must never spawn against a state directory that sits on
NFS, or on a filesystem ``mounts.is_path_on_nfs`` could not classify (``None``
- e.g. an autofs trap). Both cases raise ``NetworkStateDirError`` before any
state dir, PID file, or process is created; only a confirmed ``False``
(local disk) is allowed through to the existing start path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from bakar import hashserv, mounts, prserv
from bakar.hashserv import NetworkStateDirError, daemon_state_refusal, network_state_reason

if TYPE_CHECKING:
    from pathlib import Path

from tests.conftest import make_build_config

pytestmark = pytest.mark.unit


# --- network_state_reason --------------------------------------------------


def test_network_state_reason_none_when_local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A confirmed-local state key produces no refusal."""
    monkeypatch.setattr(mounts, "is_path_on_nfs", lambda _p: False)

    assert network_state_reason(tmp_path, service="hashserv", setting="bb_hashserve") is None


def test_network_state_reason_nfs_names_filesystem_and_fix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A confirmed-NFS state key names the state dir, ``nfs``, and the fix."""
    monkeypatch.setattr(mounts, "is_path_on_nfs", lambda _p: True)

    reason = network_state_reason(tmp_path, service="hashserv", setting="bb_hashserve")

    assert reason is not None
    assert str(tmp_path / ".bakar") in reason
    assert "nfs" in reason
    assert "bb_hashserve" in reason


def test_network_state_reason_undetermined_names_undetermined_filesystem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An undetermined (``None``) verdict is refused, not treated as safe."""
    monkeypatch.setattr(mounts, "is_path_on_nfs", lambda _p: None)

    reason = network_state_reason(tmp_path, service="prserv", setting="prserv_host")

    assert reason is not None
    assert "an undetermined filesystem" in reason
    assert "prserv_host" in reason


def test_network_state_reason_classifies_state_subdir_not_state_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``state_key`` itself may be local while its ``.bakar`` subdirectory is
    a symlink, bind mount, or nested mount pointing at NFS - the guard must
    classify the actual daemon state directory, not the parent (Bug 2)."""
    state_dir = tmp_path / ".bakar"

    def fake_is_path_on_nfs(path: Path) -> bool | None:
        return path == state_dir  # only the subdirectory is NFS

    monkeypatch.setattr(mounts, "is_path_on_nfs", fake_is_path_on_nfs)

    reason = network_state_reason(tmp_path, service="hashserv", setting="bb_hashserve")

    assert reason is not None
    assert "nfs" in reason


def test_network_state_reason_local_state_key_with_nfs_subdir_still_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``state_key`` that itself reads as confirmed-local must NOT rescue a
    refusal when the subdirectory the daemon actually writes into is NFS -
    classifying only the parent would silently let a network-backed daemon
    start (the bug the tie-break flagged)."""

    def fake_is_path_on_nfs(path: Path) -> bool | None:
        if path == tmp_path:
            return False
        if path == tmp_path / ".bakar":
            return True
        raise AssertionError(f"unexpected path classified: {path}")

    monkeypatch.setattr(mounts, "is_path_on_nfs", fake_is_path_on_nfs)

    assert network_state_reason(tmp_path, service="hashserv", setting="bb_hashserve") is not None


# --- hashserv.ensure_running hard-stop -------------------------------------


def test_hashserv_ensure_running_raises_when_only_state_subdir_is_nfs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same guarantee through the hard-stop wired into ``ensure_running``."""
    state_dir = tmp_path / ".bakar"

    def fake_is_path_on_nfs(path: Path) -> bool | None:
        return path == state_dir

    monkeypatch.setattr(mounts, "is_path_on_nfs", fake_is_path_on_nfs)

    with pytest.raises(NetworkStateDirError):
        hashserv.ensure_running(tmp_path, binary_root=tmp_path)

    assert not state_dir.exists()


def test_hashserv_ensure_running_raises_on_nfs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mounts, "is_path_on_nfs", lambda _p: True)

    with pytest.raises(NetworkStateDirError):
        hashserv.ensure_running(tmp_path, binary_root=tmp_path)

    assert not (tmp_path / ".bakar").exists()


def test_hashserv_ensure_running_raises_on_undetermined(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mounts, "is_path_on_nfs", lambda _p: None)

    with pytest.raises(NetworkStateDirError):
        hashserv.ensure_running(tmp_path, binary_root=tmp_path)

    assert not (tmp_path / ".bakar").exists()
    assert not (tmp_path / ".bakar" / "hashserv.pid").exists()


def test_hashserv_ensure_running_local_proceeds_to_existing_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A confirmed-local state key skips the guard and reaches the real logic.

    No binary is synced under ``binary_root`` here, so the existing
    ``_find_binary`` miss path returns ``None`` - proof the call passed the
    guard rather than raising, without needing to stub a real daemon spawn.
    """
    monkeypatch.setattr(mounts, "is_path_on_nfs", lambda _p: False)

    result = hashserv.ensure_running(tmp_path, binary_root=tmp_path)

    assert result is None
    assert not (tmp_path / ".bakar").exists()


# --- prserv.ensure_running hard-stop ---------------------------------------


def test_prserv_ensure_running_raises_on_nfs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mounts, "is_path_on_nfs", lambda _p: True)

    with pytest.raises(NetworkStateDirError):
        prserv.ensure_running(tmp_path, binary_root=tmp_path)

    assert not (tmp_path / ".bakar").exists()


def test_prserv_ensure_running_raises_on_undetermined(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mounts, "is_path_on_nfs", lambda _p: None)

    with pytest.raises(NetworkStateDirError):
        prserv.ensure_running(tmp_path, binary_root=tmp_path)

    assert not (tmp_path / ".bakar").exists()


def test_prserv_ensure_running_local_proceeds_to_existing_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No binary synced under ``binary_root`` - proves it reached the miss path."""
    monkeypatch.setattr(mounts, "is_path_on_nfs", lambda _p: False)

    result = prserv.ensure_running(tmp_path, binary_root=tmp_path)

    assert result is None
    assert not (tmp_path / ".bakar").exists()


# --- daemon_state_refusal ---------------------------------------------------


def test_daemon_state_refusal_none_when_central_endpoints_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Both services pointed at their central endpoint - never checked at all."""
    monkeypatch.setattr(mounts, "is_path_on_nfs", lambda _p: True)  # would refuse if checked
    monkeypatch.delenv("SSTATE_DIR", raising=False)
    cfg = make_build_config(
        workspace=tmp_path,
        use_hashequiv=True,
        host_mode=True,
        bb_hashserve="192.168.8.174:8686",
        prserv_host="192.168.8.174:8585",
    )

    assert daemon_state_refusal(cfg) is None


def test_daemon_state_refusal_hashserv_reason_when_unset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``bb_hashserve`` unset with hashequiv on - the hashserv reason surfaces."""
    monkeypatch.setattr(mounts, "is_path_on_nfs", lambda _p: True)
    monkeypatch.delenv("SSTATE_DIR", raising=False)
    cfg = make_build_config(
        workspace=tmp_path,
        use_hashequiv=True,
        host_mode=True,
        bb_hashserve=None,
        prserv_host="192.168.8.174:8585",
    )

    reason = daemon_state_refusal(cfg)

    assert reason is not None
    assert "bb_hashserve" in reason


def test_daemon_state_refusal_prserv_reason_when_unset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``prserv_host`` unset in host mode - the prserv reason surfaces."""
    monkeypatch.setattr(mounts, "is_path_on_nfs", lambda _p: True)
    monkeypatch.delenv("SSTATE_DIR", raising=False)
    cfg = make_build_config(
        workspace=tmp_path,
        use_hashequiv=True,
        host_mode=True,
        bb_hashserve="192.168.8.174:8686",
        prserv_host=None,
    )

    reason = daemon_state_refusal(cfg)

    assert reason is not None
    assert "prserv_host" in reason


def test_daemon_state_refusal_fallback_to_bsp_root_still_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ``sstate_dir`` configured - ``hashserv_state_key`` falls back to
    ``bsp_root``, and a refusal still fires when that fallback path is on NFS.
    """
    monkeypatch.delenv("SSTATE_DIR", raising=False)
    cfg = make_build_config(
        workspace=tmp_path,
        use_hashequiv=True,
        host_mode=True,
        bb_hashserve=None,
        prserv_host="192.168.8.174:8585",
        sstate_dir=None,
    )
    assert cfg.hashserv_state_key == cfg.bsp_root

    def fake_is_path_on_nfs(path: Path) -> bool | None:
        # network_state_reason classifies the actual daemon state directory
        # (state_key / ".bakar"), not state_key itself (Bug 2) - the fake
        # must match on that subdirectory, not the bare fallback root.
        return path == cfg.bsp_root / ".bakar"

    monkeypatch.setattr(mounts, "is_path_on_nfs", fake_is_path_on_nfs)

    reason = daemon_state_refusal(cfg)

    assert reason is not None
    assert "bb_hashserve" in reason
