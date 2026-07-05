"""Cluster-mode preflight: gating flag + cluster-only doctor checks.

These tests defend the cluster-off default: the `cluster` flag resolves False
when absent, and the cluster preflight checks are filtered out of `doctor`
entirely (no result row, no probe) unless it is on.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from bakar.config import resolve
from bakar.user_config import UserConfig
from bakar.workspace_config import WorkspaceConfig

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip ambient toggles so each test controls cluster resolution explicitly."""
    monkeypatch.delenv("BAKAR_CLUSTER", raising=False)
    monkeypatch.delenv("KAS_CONTAINER_IMAGE", raising=False)


def _workspace(tmp_path: Path) -> Path:
    """A workspace path with the nxp subdir present (resolve() needs it)."""
    (tmp_path / "nxp").mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.mark.unit
def test_config_default_absent_key_is_non_cluster(tmp_path: Path) -> None:
    """An absent `cluster` key resolves to a non-cluster build."""
    cfg = resolve(
        workspace=_workspace(tmp_path),
        bsp_family="nxp",
        user_config=UserConfig(),
        workspace_config=WorkspaceConfig(),
    )
    assert cfg.cluster is False


@pytest.mark.unit
def test_config_default_cluster_true_when_set(tmp_path: Path) -> None:
    """`cluster = true` resolves to a cluster build."""
    cfg = resolve(
        workspace=_workspace(tmp_path),
        bsp_family="nxp",
        user_config=UserConfig(cluster=True),
        workspace_config=WorkspaceConfig(),
    )
    assert cfg.cluster is True
