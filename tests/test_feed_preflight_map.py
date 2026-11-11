"""Map-staleness check: a leaf-recipe build does not rewrite avocado-repo.map.

Spike 1.2 measured the map's mtime a day older than a finished leaf build, so a
sync from it can render a feed that omits the newly built packages.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from bakar import feed_preflight
from bakar.diagnostics import Severity, Status

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


def test_stale_map_fails(tmp_path: Path) -> None:
    deploy = _deploy(tmp_path, map_time=_MAP_TIME, rpm_time=_MAP_TIME + 86_400)

    result = feed_preflight.check_map_fresh(deploy)

    assert result.status is Status.FAIL
    assert result.severity is Severity.BLOCK
    assert result.name == _NAME
    assert "older" in result.message


def test_fresh_map_passes(tmp_path: Path) -> None:
    deploy = _deploy(tmp_path, map_time=_MAP_TIME + 10, rpm_time=_MAP_TIME)

    result = feed_preflight.check_map_fresh(deploy)

    assert result.status is Status.PASS
    assert result.name == _NAME


def test_map_equal_to_newest_rpm_passes(tmp_path: Path) -> None:
    deploy = _deploy(tmp_path, map_time=_MAP_TIME, rpm_time=_MAP_TIME)

    assert feed_preflight.check_map_fresh(deploy).status is Status.PASS


def test_no_rpms_passes(tmp_path: Path) -> None:
    deploy = _deploy(tmp_path, map_time=_MAP_TIME, rpm_time=None)

    assert feed_preflight.check_map_fresh(deploy).status is Status.PASS


def test_missing_map_is_left_to_build_output_check(tmp_path: Path) -> None:
    deploy = _deploy(tmp_path, map_time=None, rpm_time=_MAP_TIME)

    assert feed_preflight.check_map_fresh(deploy).status is Status.PASS


def test_preflight_blocks_on_stale_map(tmp_path: Path) -> None:
    deploy = _deploy(tmp_path, map_time=_MAP_TIME, rpm_time=_MAP_TIME + 86_400)

    results = feed_preflight.preflight(build=feed_preflight.PreflightBuild(deploy_dir=deploy))

    assert any(r.name == _NAME and r.status is Status.FAIL for r in feed_preflight.blocking(results))
