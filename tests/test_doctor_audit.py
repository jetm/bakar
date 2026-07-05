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
    base: dict[str, object] = {
        "workspace": Path("/tmp"),
        "bsp_family": "nxp",
        "machine": "m",
        "distro": "d",
        "image": "i",
        "manifest": "x.xml",
        "repo_url": "https://example.com",
        "repo_branch": "main",
        "kas_container_image": "img:latest",
    }
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


@pytest.mark.unit
def test_docker_membership_is_exactly_the_daemon_checks() -> None:
    """_DOCKER_CHECKS holds exactly the docker-daemon checks; host-pure checks stay."""
    from bakar.diagnostics import (
        _DOCKER_CHECKS,
        SHARED_CHECKS,
        check_cache_dirs,
        check_container_bitbake,
        check_container_image,
        check_docker_daemon,
        check_docker_storage_driver,
        check_docker_ulimits,
        check_docker_version,
        check_hashserv,
        check_psi_support,
        check_workspace_filesystem,
    )

    # Every docker-dependent check is registered in SHARED_CHECKS.
    assert set(_DOCKER_CHECKS) <= set(SHARED_CHECKS)
    # Membership is exactly the six docker-daemon-dependent checks.
    assert set(_DOCKER_CHECKS) == {
        check_docker_daemon,
        check_container_image,
        check_container_bitbake,
        check_docker_ulimits,
        check_docker_version,
        check_docker_storage_driver,
    }
    # Host-pure checks are NOT members, so host mode does not drop them.
    for check in (check_psi_support, check_hashserv, check_cache_dirs, check_workspace_filesystem):
        assert check not in _DOCKER_CHECKS
    # Host mode drops exactly _DOCKER_CHECKS and nothing else.
    host_checks = tuple(c for c in SHARED_CHECKS if c not in _DOCKER_CHECKS)
    assert set(SHARED_CHECKS) - set(host_checks) == set(_DOCKER_CHECKS)
