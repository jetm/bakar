"""The ``daemon-state`` doctor check (daemon-state-filesystem-guard).

Covers ``check_daemon_state``: SKIP when no per-workspace daemon is in use
(either service points at a central endpoint, or is off entirely), FAIL at
BLOCK naming the unsafe setting when ``hashserv.daemon_state_refusal`` finds
one, and PASS when the state directory is confirmed local. The check
delegates its NFS classification to ``bakar.mounts.is_path_on_nfs`` (patched
here) through ``hashserv.daemon_state_refusal`` rather than re-deriving it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from bakar import mounts
from bakar.diagnostics import Severity, Status, check_daemon_state

if TYPE_CHECKING:
    from pathlib import Path

from tests.conftest import make_build_config

pytestmark = pytest.mark.unit


def test_nfs_no_central_tier_fails_block_naming_bb_hashserve(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mounts, "is_path_on_nfs", lambda _p: True)
    monkeypatch.delenv("SSTATE_DIR", raising=False)
    cfg = make_build_config(
        workspace=tmp_path,
        sstate_dir="/cache/sstate",
        use_hashequiv=True,
        host_mode=False,
        bb_hashserve=None,
    )

    result = check_daemon_state(cfg)

    assert result.name == "daemon-state"
    assert result.status == Status.FAIL
    assert result.severity == Severity.BLOCK
    assert "bb_hashserve" in result.message
    assert result.fix_hint is not None
    assert "bb_hashserve" in result.fix_hint


def test_central_tier_configured_skips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mounts, "is_path_on_nfs", lambda _p: True)  # would refuse if checked
    monkeypatch.delenv("SSTATE_DIR", raising=False)
    cfg = make_build_config(
        workspace=tmp_path,
        sstate_dir="/cache/sstate",
        use_hashequiv=True,
        bb_hashserve="192.168.8.174:8686",
        host_mode=True,
        prserv_host="192.168.8.174:8585",
    )

    result = check_daemon_state(cfg)

    assert result.name == "daemon-state"
    assert result.status == Status.SKIP
    assert "no per-workspace hashserv/prserv daemon in use" in result.message


def test_local_disk_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mounts, "is_path_on_nfs", lambda _p: False)
    monkeypatch.delenv("SSTATE_DIR", raising=False)
    cfg = make_build_config(
        workspace=tmp_path,
        sstate_dir="/cache/sstate",
        use_hashequiv=True,
        host_mode=False,
        bb_hashserve=None,
    )

    result = check_daemon_state(cfg)

    assert result.name == "daemon-state"
    assert result.status == Status.PASS
    assert result.severity == Severity.BLOCK


def test_no_sstate_dir_falls_back_to_bsp_root_and_still_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``cfg.sstate_dir`` unset resolves ``hashserv_state_key`` to ``bsp_root``."""
    monkeypatch.setattr(mounts, "is_path_on_nfs", lambda _p: True)
    monkeypatch.delenv("SSTATE_DIR", raising=False)
    cfg = make_build_config(
        workspace=tmp_path,
        sstate_dir=None,
        use_hashequiv=True,
        host_mode=False,
        bb_hashserve=None,
    )

    result = check_daemon_state(cfg)

    assert result.name == "daemon-state"
    assert result.status == Status.FAIL
    assert result.severity == Severity.BLOCK
    assert "bb_hashserve" in result.message
