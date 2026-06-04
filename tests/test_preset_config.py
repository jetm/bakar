from __future__ import annotations

import pytest

from bakar.preset_config import PresetEntry

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _nxp_single(**kwargs) -> PresetEntry:
    """Minimal valid nxp single-release preset."""
    defaults = {
        "name": "test-nxp",
        "family": "nxp",
        "manifest": "imx-6.6.52-2.2.2.xml",
        "branch": "lf-6.6.y",
    }
    defaults.update(kwargs)
    return PresetEntry(**defaults)


def _bbsetup_single(**kwargs) -> PresetEntry:
    """Minimal valid bbsetup single-release preset."""
    defaults = {
        "name": "test-bbsetup",
        "family": "bbsetup",
        "kas_yaml": "conf/qemux86-64.yml",
    }
    defaults.update(kwargs)
    return PresetEntry(**defaults)


# ---------------------------------------------------------------------------
# Family validation
# ---------------------------------------------------------------------------


def test_valid_families_accepted():
    for family in ("nxp", "ti", "generic", "bbsetup"):
        entry = PresetEntry(name="x", family=family, manifest="any.xml", branch="main")
        assert entry.family == family


def test_invalid_family_raises():
    with pytest.raises(ValueError) as exc_info:
        PresetEntry(name="bad", family="rockchip", manifest="any.xml", branch="main")
    msg = str(exc_info.value)
    assert "rockchip" in msg
    # all valid families should be listed
    for f in ("nxp", "ti", "generic", "bbsetup"):
        assert f in msg


def test_invalid_family_message_shape():
    # Error message should mirror VendorEntry: "family must be one of [...], got '...'"
    with pytest.raises(ValueError) as exc_info:
        PresetEntry(name="mypreset", family="rk3588", manifest="any.xml", branch="main")
    msg = str(exc_info.value)
    assert "mypreset" in msg
    assert "must be one of" in msg
    assert "got 'rk3588'" in msg


# ---------------------------------------------------------------------------
# Single-release accepted
# ---------------------------------------------------------------------------


def test_nxp_single_release_accepted():
    entry = _nxp_single()
    assert entry.manifest == "imx-6.6.52-2.2.2.xml"
    assert entry.manifests == []


def test_ti_single_release_accepted():
    entry = PresetEntry(name="ti-test", family="ti", manifest="processor-sdk-09.02.xml", branch="main")
    assert entry.manifest == "processor-sdk-09.02.xml"


def test_bbsetup_single_release_accepted():
    entry = _bbsetup_single()
    assert entry.kas_yaml == "conf/qemux86-64.yml"
    assert entry.kas_yamls == []


def test_generic_single_release_accepted():
    entry = PresetEntry(name="gen-test", family="generic", kas_yaml="my.yml")
    assert entry.kas_yaml == "my.yml"


# ---------------------------------------------------------------------------
# Multi-release accepted
# ---------------------------------------------------------------------------


def test_nxp_multi_release_accepted():
    entry = PresetEntry(
        name="nxp-multi",
        family="nxp",
        manifests=["imx-6.6.52-2.2.2.xml", "imx-6.1.55-2.2.0.xml"],
        branches=["lf-6.6.y", "lf-6.1.y"],
    )
    assert len(entry.manifests) == 2
    assert len(entry.branches) == 2


def test_bbsetup_multi_release_accepted():
    entry = PresetEntry(
        name="bbsetup-multi",
        family="bbsetup",
        kas_yamls=["conf/qemux86-64.yml", "conf/raspberrypi4.yml"],
    )
    assert len(entry.kas_yamls) == 2


# ---------------------------------------------------------------------------
# Both single + multi raises
# ---------------------------------------------------------------------------


def test_manifest_and_manifests_raises():
    with pytest.raises(ValueError) as exc_info:
        PresetEntry(
            name="conflict",
            family="nxp",
            manifest="imx-6.6.52-2.2.2.xml",
            manifests=["imx-6.6.52-2.2.2.xml"],
            branch="lf-6.6.y",
            branches=["lf-6.6.y"],
        )
    assert "single-release or multi-release fields, not both" in str(exc_info.value)


def test_kas_yaml_and_kas_yamls_raises():
    with pytest.raises(ValueError) as exc_info:
        PresetEntry(
            name="conflict",
            family="bbsetup",
            kas_yaml="conf/a.yml",
            kas_yamls=["conf/a.yml", "conf/b.yml"],
        )
    assert "single-release or multi-release fields, not both" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Neither single nor multi raises
# ---------------------------------------------------------------------------


def test_no_build_target_raises():
    with pytest.raises(ValueError) as exc_info:
        PresetEntry(name="empty", family="nxp")
    assert "specifies no build target" in str(exc_info.value)


def test_no_build_target_bbsetup_raises():
    with pytest.raises(ValueError) as exc_info:
        PresetEntry(name="empty-bb", family="bbsetup")
    assert "specifies no build target" in str(exc_info.value)


# ---------------------------------------------------------------------------
# nxp/ti multi-release length mismatch raises
# ---------------------------------------------------------------------------


def test_nxp_manifests_branches_length_mismatch_raises():
    with pytest.raises(ValueError) as exc_info:
        PresetEntry(
            name="mismatch",
            family="nxp",
            manifests=["imx-6.6.52-2.2.2.xml", "imx-6.1.55-2.2.0.xml"],
            branches=["lf-6.6.y"],
        )
    msg = str(exc_info.value)
    assert "2" in msg  # 2 manifests
    assert "1" in msg  # 1 branch


def test_ti_manifests_branches_length_mismatch_raises():
    with pytest.raises(ValueError) as exc_info:
        PresetEntry(
            name="ti-mismatch",
            family="ti",
            manifests=["sdk-09.xml", "sdk-10.xml", "sdk-11.xml"],
            branches=["main", "develop"],
        )
    msg = str(exc_info.value)
    assert "3" in msg
    assert "2" in msg


def test_bbsetup_kas_yamls_no_branch_check():
    # bbsetup multi-release uses kas_yamls, not manifests - no branches required
    entry = PresetEntry(
        name="bb-multi",
        family="bbsetup",
        kas_yamls=["conf/a.yml", "conf/b.yml", "conf/c.yml"],
    )
    assert len(entry.kas_yamls) == 3


def test_generic_kas_yamls_no_branch_check():
    # generic multi-release same: no branches enforced
    entry = PresetEntry(
        name="gen-multi",
        family="generic",
        kas_yamls=["x.yml"],
    )
    assert len(entry.kas_yamls) == 1
