"""The shared cache-mount refusal wording matches the per-module copies it replaces."""

from pathlib import Path

import pytest

from bakar import diagnostics, mounts
from bakar.steps import kas_build


def _st(state, *, fstype="nfs4", source="srv:/x", fstab_source="srv:/x", declared_nfs=True, detail="", label="sstate"):  # noqa: PLR0913
    return mounts.CacheMountStatus(
        label=label,
        path=Path("/cache/sstate"),
        state=state,
        fstype=fstype,
        mountpoint="/cache",
        source=source,
        declared_nfs=declared_nfs,
        fstab_source=fstab_source,
        detail=detail,
    )


CASES = [
    _st("unresponsive"),
    _st("error", detail="Stale file handle"),
    _st("missing", detail=""),
    _st("missing", detail="no such dir"),
    _st("ready", fstype="ext4", source="/dev/sda1", fstab_source="nas:/exp"),
    _st("ready", fstype="ext4", source=None, fstab_source=None),
]


@pytest.mark.parametrize("status", CASES)
def test_launch_gate_and_doctor_share_text(status, monkeypatch):
    """The launch-gate refusal and the doctor check say the same thing for the same status."""
    doctor = diagnostics._cache_mounts_result([status])
    gate = kas_build.mounts.cache_mount_blocking_detail([status])
    assert doctor.message == gate
    assert not hasattr(diagnostics, "_cache_mount_server")
    assert not hasattr(kas_build, "_cache_mount_server")


def test_blocking_detail_matches_diagnostics_text():
    result = diagnostics._cache_mounts_result(CASES[:2])
    assert mounts.cache_mount_blocking_detail(CASES[:2]) == result.message


def test_none_when_nothing_blocks():
    ok = _st("ready", declared_nfs=False, fstype="ext4")
    assert mounts.cache_mount_blocking_detail([ok]) is None
    assert mounts.cache_mount_blocking_detail([]) is None


def test_hint_matches_diagnostics_fix_hint():
    result = diagnostics._cache_mounts_result([CASES[0]])
    assert result.fix_hint == mounts.CACHE_MOUNT_REMEDIATION_HINT
