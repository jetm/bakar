"""Audit-half doctor invariants: sccache-dist gating and _DOCKER_CHECKS membership.

These lock behavior the cluster-preflight change relies on: the sccache-dist
check must not probe a scheduler a ccache-only build never uses, and host mode
(the default) must drop exactly the docker-daemon-dependent checks.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bakar.config import BuildConfig
from bakar.diagnostics import Severity, Status, check_sccache_dist


def _cfg(**over: object) -> BuildConfig:
    base: dict[str, object] = dict(
        workspace=Path("/tmp"),
        bsp_family="nxp",
        machine="m",
        distro="d",
        image="i",
        manifest="x.xml",
        repo_url="https://example.com",
        repo_branch="main",
        kas_container_image="img:latest",
    )
    base.update(over)
    return BuildConfig(**base)  # type: ignore[arg-type]


@pytest.mark.unit
def test_sccache_gated_skips_without_probe_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disabled sccache-dist returns SKIP and attempts no scheduler connection."""

    def _boom(*_a: object, **_k: object) -> object:
        raise AssertionError("check_sccache_dist attempted a connection while sccache-dist disabled")

    monkeypatch.setattr("bakar.diagnostics.socket.create_connection", _boom)
    result = check_sccache_dist(_cfg(sccache_dist=False))
    assert result.status == Status.SKIP


@pytest.mark.unit
def test_sccache_gated_proceeds_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enabled sccache-dist opens the gate: the host-mode check proceeds past SKIP.

    With the sccache binary absent it fails at BLOCK, proving the gate did not
    short-circuit to the disabled SKIP.
    """
    monkeypatch.setattr("bakar.diagnostics.shutil.which", lambda _n: None)
    result = check_sccache_dist(_cfg(sccache_dist=True, host_mode=True))
    assert result.status == Status.FAIL
    assert result.severity == Severity.BLOCK
