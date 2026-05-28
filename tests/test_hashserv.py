"""Unit tests for bspctl.hashserv pure helpers.

Covers the deterministic port derivation and the workspace-pinned
binary lookup. The binary lookup explicitly must NOT fall through to
host PATH - a PATH-mismatch daemon would speak a different wire
protocol than the workspace bitbake and silently corrupt the
equivalence cache.
"""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING

import pytest

from bspctl.hashserv import _find_binary, _workspace_port

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


def test_workspace_port_is_deterministic(tmp_path: Path) -> None:
    """Same workspace path must always derive the same port."""
    assert _workspace_port(tmp_path) == _workspace_port(tmp_path)


def test_workspace_port_in_ephemeral_range(tmp_path: Path) -> None:
    """Derived port must fall in the IANA ephemeral range (49152-65534)."""
    port = _workspace_port(tmp_path)
    assert 49152 <= port < 65535


def test_workspace_port_differs_across_paths(tmp_path: Path) -> None:
    """Two distinct workspace paths should generally yield different ports.

    Collision probability is ~1-in-16383; two adjacent tmp_path siblings
    are independent SHA-256 inputs so a collision here would be a real
    bug, not just bad luck.
    """
    workspace_a = tmp_path / "wsA"
    workspace_b = tmp_path / "wsB"
    workspace_a.mkdir()
    workspace_b.mkdir()
    assert _workspace_port(workspace_a) != _workspace_port(workspace_b)


def test_find_binary_workspace_hit(tmp_path: Path) -> None:
    """When the workspace binary exists, its path is returned."""
    binary_path = tmp_path / "sources" / "poky" / "bitbake" / "bin" / "bitbake-hashserv"
    binary_path.parent.mkdir(parents=True)
    binary_path.write_text("#!/bin/sh\n")
    binary_path.chmod(0o755)

    result = _find_binary(tmp_path)

    assert result == binary_path


def test_find_binary_returns_none_when_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the workspace binary is absent, return None - never PATH.

    Booby-trap ``shutil.which`` so any host-PATH fallback raises; the
    spec pins the workspace path and a PATH-mismatch daemon would speak
    a different protocol than the workspace bitbake.
    """

    def _explode(*_args: object, **_kwargs: object) -> None:
        msg = "shutil.which must not be consulted - hashserv is workspace-pinned"
        raise AssertionError(msg)

    monkeypatch.setattr(shutil, "which", _explode)

    result = _find_binary(tmp_path)

    assert result is None
