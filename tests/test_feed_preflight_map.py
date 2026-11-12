"""Preflight must not block on a stale avocado-repo.map.

A leaf-recipe build does not rewrite the map (spike 1.2), so the staleness check
lives in the refresh pipeline, where it compares only pre-existing packages.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from bakar import feed_preflight
from bakar.diagnostics import Status

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit

_MAP_TIME = 1_000_000
_NAME = "avocado-repo.map"


def _deploy(tmp_path: Path, *, map_time: int | None, rpm_time: int | None) -> Path:
    deploy = tmp_path / "deploy" / "rpm"
    (deploy / "core2_64").mkdir(parents=True)
    if map_time is not None:
        repo_map = deploy / _NAME
        repo_map.write_text("core2_64 file:///x\n")
        os.utime(repo_map, (map_time, map_time))
    if rpm_time is not None:
        rpm = deploy / "core2_64" / "htop-3.0-r0.core2_64.rpm"
        rpm.write_bytes(b"rpm")
        os.utime(rpm, (rpm_time, rpm_time))
    return deploy


def test_preflight_does_not_block_on_stale_map(tmp_path: Path) -> None:
    # The staleness check belongs to the refresh command only: `bakar build --feed`
    # runs preflight before the build that would rewrite the map.
    deploy = _deploy(tmp_path, map_time=_MAP_TIME, rpm_time=_MAP_TIME + 86_400)

    results = feed_preflight.preflight(build=feed_preflight.PreflightBuild(deploy_dir=deploy))

    assert not any(r.name == _NAME and r.status is Status.FAIL for r in results)
