from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from bakar.preset_config import PresetEntry, PresetSpec, load_presets

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
    for family in ("nxp", "ti"):
        entry = PresetEntry(name="x", family=family, manifest="any.xml", branch="main")
        assert entry.family == family
    for family in ("generic", "bbsetup"):
        entry = PresetEntry(name="x", family=family, kas_yaml="meta/kas/machine.yml")
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


# ---------------------------------------------------------------------------
# PresetEntry.resolve()
# ---------------------------------------------------------------------------


def test_resolve_nxp_single_returns_one_spec():
    entry = _nxp_single(machine="imx8mp-var-dart", distro="fsl-imx-xwayland", image="fsl-image-gui")
    specs = entry.resolve()
    assert len(specs) == 1
    spec = specs[0]
    assert isinstance(spec, PresetSpec)
    assert spec.family == "nxp"
    assert spec.manifest == "imx-6.6.52-2.2.2.xml"
    assert spec.branch == "lf-6.6.y"
    assert spec.machine == "imx8mp-var-dart"
    assert spec.distro == "fsl-imx-xwayland"
    assert spec.image == "fsl-image-gui"
    assert spec.kas_yaml is None


def test_resolve_ti_single_returns_one_spec():
    entry = PresetEntry(
        name="ti-single",
        family="ti",
        manifest="processor-sdk-09.02.xml",
        branch="main",
        machine="am62xx-evm",
    )
    specs = entry.resolve()
    assert len(specs) == 1
    assert specs[0].manifest == "processor-sdk-09.02.xml"
    assert specs[0].branch == "main"
    assert specs[0].machine == "am62xx-evm"


def test_resolve_nxp_multi_returns_one_per_release():
    entry = PresetEntry(
        name="nxp-multi",
        family="nxp",
        manifests=["imx-6.6.52-2.2.2.xml", "imx-6.1.55-2.2.0.xml"],
        branches=["lf-6.6.y", "lf-6.1.y"],
        machine="imx8mp-var-dart",
    )
    specs = entry.resolve()
    assert len(specs) == 2
    assert specs[0].manifest == "imx-6.6.52-2.2.2.xml"
    assert specs[0].branch == "lf-6.6.y"
    assert specs[1].manifest == "imx-6.1.55-2.2.0.xml"
    assert specs[1].branch == "lf-6.1.y"
    # machine propagates to all
    assert all(s.machine == "imx8mp-var-dart" for s in specs)


def test_resolve_nxp_multi_kas_yaml_is_none():
    entry = PresetEntry(
        name="nxp-multi",
        family="nxp",
        manifests=["imx-6.6.52-2.2.2.xml", "imx-6.1.55-2.2.0.xml"],
        branches=["lf-6.6.y", "lf-6.1.y"],
    )
    specs = entry.resolve()
    assert all(s.kas_yaml is None for s in specs)


def test_resolve_bbsetup_single_returns_one_spec():
    entry = _bbsetup_single(machine="qemux86-64", image="avocado-os-dev")
    specs = entry.resolve()
    assert len(specs) == 1
    spec = specs[0]
    assert spec.family == "bbsetup"
    assert isinstance(spec.kas_yaml, Path)
    assert spec.kas_yaml == Path("conf/qemux86-64.yml")
    assert spec.machine == "qemux86-64"
    assert spec.image == "avocado-os-dev"
    assert spec.manifest is None


def test_resolve_generic_single_returns_one_spec():
    entry = PresetEntry(name="gen", family="generic", kas_yaml="my-board.yml", machine="myboard")
    specs = entry.resolve()
    assert len(specs) == 1
    assert specs[0].kas_yaml == Path("my-board.yml")
    assert specs[0].machine == "myboard"


def test_resolve_bbsetup_multi_returns_one_per_yaml():
    entry = PresetEntry(
        name="bb-all",
        family="bbsetup",
        kas_yamls=["conf/qemux86-64.yml", "conf/raspberrypi4.yml", "conf/beaglebone.yml"],
        machine="qemux86-64",
        image="avocado-os",
    )
    specs = entry.resolve()
    assert len(specs) == 3
    assert specs[0].kas_yaml == Path("conf/qemux86-64.yml")
    assert specs[1].kas_yaml == Path("conf/raspberrypi4.yml")
    assert specs[2].kas_yaml == Path("conf/beaglebone.yml")
    assert all(isinstance(s.kas_yaml, Path) for s in specs)


def test_resolve_bbsetup_multi_manifest_is_none():
    entry = PresetEntry(
        name="bb-multi",
        family="bbsetup",
        kas_yamls=["a.yml", "b.yml"],
    )
    specs = entry.resolve()
    assert all(s.manifest is None for s in specs)


def test_resolve_generic_multi_returns_correct_count():
    entry = PresetEntry(
        name="gen-multi",
        family="generic",
        kas_yamls=["r1.yml", "r2.yml"],
    )
    specs = entry.resolve()
    assert len(specs) == 2
    assert specs[0].family == "generic"
    assert specs[1].family == "generic"


def test_resolve_nxp_single_fields_none_when_not_set():
    entry = _nxp_single()
    spec = entry.resolve()[0]
    assert spec.machine is None
    assert spec.distro is None
    assert spec.image is None


# ---------------------------------------------------------------------------
# load_presets()
# ---------------------------------------------------------------------------

_NXP_PRESET_TOML = """\
[[presets]]
name = "imx8mp-scarthgap"
family = "nxp"
manifest = "imx-6.6.52-2.2.2.xml"
branch = "lf-6.6.y"
machine = "imx8mp-var-dart"
"""

_BBSETUP_PRESET_TOML = """\
[[presets]]
name = "avocado-qemux86-64"
family = "bbsetup"
kas_yaml = "conf/qemux86-64.yml"
machine = "qemux86-64"
image = "avocado-os-dev"
"""

_TWO_PRESET_TOML = _NXP_PRESET_TOML + "\n" + _BBSETUP_PRESET_TOML


def test_load_presets_returns_empty_when_no_files(tmp_path):
    result = load_presets(
        config_path=tmp_path / "config.toml",
        vendors_path=tmp_path / "vendors.toml",
    )
    assert result == []


def test_load_presets_absent_config_returns_empty(tmp_path):
    vendors = tmp_path / "vendors.toml"
    vendors.write_text("[vendors]\n")
    result = load_presets(config_path=tmp_path / "missing.toml", vendors_path=vendors)
    assert result == []


def test_load_presets_config_no_presets_section_returns_empty(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('[build]\nmachine = "qemux86-64"\n')
    result = load_presets(config_path=config, vendors_path=tmp_path / "missing.toml")
    assert result == []


def test_load_presets_single_nxp_preset(tmp_path):
    config = tmp_path / "config.toml"
    config.write_bytes(_NXP_PRESET_TOML.encode())
    result = load_presets(config_path=config, vendors_path=tmp_path / "missing.toml")
    assert len(result) == 1
    assert isinstance(result[0], PresetEntry)
    assert result[0].name == "imx8mp-scarthgap"
    assert result[0].family == "nxp"


def test_load_presets_single_bbsetup_preset(tmp_path):
    config = tmp_path / "config.toml"
    config.write_bytes(_BBSETUP_PRESET_TOML.encode())
    result = load_presets(config_path=config, vendors_path=tmp_path / "missing.toml")
    assert len(result) == 1
    assert result[0].family == "bbsetup"


def test_load_presets_two_presets_from_config(tmp_path):
    config = tmp_path / "config.toml"
    config.write_bytes(_TWO_PRESET_TOML.encode())
    result = load_presets(config_path=config, vendors_path=tmp_path / "missing.toml")
    assert len(result) == 2


def test_load_presets_merges_config_and_vendors(tmp_path):
    config = tmp_path / "config.toml"
    config.write_bytes(_NXP_PRESET_TOML.encode())
    vendors = tmp_path / "vendors.toml"
    vendors.write_bytes(_BBSETUP_PRESET_TOML.encode())
    result = load_presets(config_path=config, vendors_path=vendors)
    assert len(result) == 2
    names = {p.name for p in result}
    assert "imx8mp-scarthgap" in names
    assert "avocado-qemux86-64" in names


def test_load_presets_duplicate_name_raises(tmp_path):
    config = tmp_path / "config.toml"
    config.write_bytes(_NXP_PRESET_TOML.encode())
    vendors = tmp_path / "vendors.toml"
    vendors.write_bytes(_NXP_PRESET_TOML.encode())
    with pytest.raises(ValueError) as exc_info:
        load_presets(config_path=config, vendors_path=vendors)
    assert "imx8mp-scarthgap" in str(exc_info.value)


def test_load_presets_duplicate_name_message_names_preset(tmp_path):
    config = tmp_path / "config.toml"
    config.write_bytes(_NXP_PRESET_TOML.encode())
    vendors = tmp_path / "vendors.toml"
    vendors.write_bytes(_NXP_PRESET_TOML.encode())
    with pytest.raises(ValueError, match="imx8mp-scarthgap"):
        load_presets(config_path=config, vendors_path=vendors)


def test_load_presets_propagates_invalid_preset_error(tmp_path):
    bad_toml = """\
[[presets]]
name = "bad-family"
family = "rockchip"
manifest = "any.xml"
branch = "main"
"""
    config = tmp_path / "config.toml"
    config.write_bytes(bad_toml.encode())
    with pytest.raises(ValueError) as exc_info:
        load_presets(config_path=config, vendors_path=tmp_path / "missing.toml")
    assert "rockchip" in str(exc_info.value)


def test_load_presets_propagates_toml_parse_error(tmp_path):
    config = tmp_path / "config.toml"
    config.write_bytes(b"[[presets]\nbroken toml [[[")
    with pytest.raises((tomllib.TOMLDecodeError, ValueError)):
        load_presets(config_path=config, vendors_path=tmp_path / "missing.toml")


def test_load_presets_vendors_only(tmp_path):
    vendors = tmp_path / "vendors.toml"
    vendors.write_bytes(_NXP_PRESET_TOML.encode())
    result = load_presets(config_path=tmp_path / "missing.toml", vendors_path=vendors)
    assert len(result) == 1
    assert result[0].name == "imx8mp-scarthgap"


def test_load_presets_returns_preset_entry_instances(tmp_path):
    config = tmp_path / "config.toml"
    config.write_bytes(_TWO_PRESET_TOML.encode())
    result = load_presets(config_path=config, vendors_path=tmp_path / "missing.toml")
    assert all(isinstance(p, PresetEntry) for p in result)


# ---------------------------------------------------------------------------
# Family-specific wrong-field raises (lines 68-87)
# ---------------------------------------------------------------------------


def test_nxp_with_kas_yaml_instead_of_manifest_raises():
    """nxp/ti must use manifest for single-release, not kas_yaml (line 68)."""
    with pytest.raises(ValueError, match="manifest"):
        PresetEntry(name="bad-nxp", family="nxp", kas_yaml="conf/board.yml")


def test_nxp_with_kas_yamls_instead_of_manifests_raises():
    """nxp/ti must use manifests for multi-release, not kas_yamls (line 73)."""
    with pytest.raises(ValueError, match="manifests"):
        PresetEntry(name="bad-nxp", family="nxp", kas_yamls=["conf/a.yml", "conf/b.yml"])


def test_bbsetup_with_manifest_instead_of_kas_yaml_raises():
    """generic/bbsetup must use kas_yaml for single-release, not manifest (line 79)."""
    with pytest.raises(ValueError, match="kas_yaml"):
        PresetEntry(name="bad-bbsetup", family="bbsetup", manifest="any.xml", branch="main")


def test_generic_with_manifests_instead_of_kas_yamls_raises():
    """generic/bbsetup must use kas_yamls for multi-release, not manifests (line 84)."""
    with pytest.raises(ValueError, match="kas_yamls"):
        PresetEntry(name="bad-generic", family="generic", manifests=["a.xml", "b.xml"], branches=["b1", "b2"])


# ---------------------------------------------------------------------------
# load_presets() edge cases: missing name field and unknown keys (lines 176, 180)
# ---------------------------------------------------------------------------


def test_load_presets_missing_name_raises(tmp_path):
    """A preset dict without 'name' raises ValueError (line 176)."""
    bad_toml = """\
[[presets]]
family = "nxp"
manifest = "imx-6.6.52-2.2.2.xml"
branch = "lf-6.6.y"
"""
    config = tmp_path / "config.toml"
    config.write_bytes(bad_toml.encode())
    with pytest.raises(ValueError, match="name"):
        load_presets(config_path=config, vendors_path=tmp_path / "missing.toml")


def test_load_presets_unknown_field_raises(tmp_path):
    """A preset dict with an unknown key raises ValueError (wraps TypeError, line 180)."""
    bad_toml = """\
[[presets]]
name = "bad-entry"
family = "nxp"
manifest = "imx-6.6.52-2.2.2.xml"
branch = "lf-6.6.y"
unknown_key = "oops"
"""
    config = tmp_path / "config.toml"
    config.write_bytes(bad_toml.encode())
    with pytest.raises(ValueError, match="Invalid preset entry"):
        load_presets(config_path=config, vendors_path=tmp_path / "missing.toml")


# ---------------------------------------------------------------------------
# vendor_preset integration with load_presets()
# ---------------------------------------------------------------------------


def test_vendor_preset_absent_section_returns_empty(tmp_path):
    """vendors.toml with no [[presets]] section: load_presets returns [] for vendor part."""
    vendors = tmp_path / "vendors.toml"
    vendors.write_text('[[vendors]]\nname = "acme"\nfamily = "nxp"\nmanifest_regex = "imx-.*\\\\.xml"\n')
    result = load_presets(config_path=tmp_path / "missing.toml", vendors_path=vendors)
    assert result == []


def test_vendor_preset_loaded_via_load_presets(tmp_path):
    """vendor_preset in vendors.toml is returned by load_presets()."""
    vendors = tmp_path / "vendors.toml"
    vendors.write_bytes(_NXP_PRESET_TOML.encode())
    result = load_presets(config_path=tmp_path / "missing.toml", vendors_path=vendors)
    assert len(result) == 1
    assert result[0].name == "imx8mp-scarthgap"
    assert isinstance(result[0], PresetEntry)


def test_vendor_preset_merged_with_user_presets(tmp_path):
    """vendor_preset entries merge with user config entries."""
    config = tmp_path / "config.toml"
    config.write_bytes(_BBSETUP_PRESET_TOML.encode())
    vendors = tmp_path / "vendors.toml"
    vendors.write_bytes(_NXP_PRESET_TOML.encode())
    result = load_presets(config_path=config, vendors_path=vendors)
    assert len(result) == 2
    names = {p.name for p in result}
    assert "imx8mp-scarthgap" in names
    assert "avocado-qemux86-64" in names


def test_vendor_preset_duplicate_name_raises(tmp_path):
    """vendor_preset with same name as user preset raises ValueError."""
    config = tmp_path / "config.toml"
    config.write_bytes(_NXP_PRESET_TOML.encode())
    vendors = tmp_path / "vendors.toml"
    vendors.write_bytes(_NXP_PRESET_TOML.encode())
    with pytest.raises(ValueError, match="imx8mp-scarthgap"):
        load_presets(config_path=config, vendors_path=vendors)


def test_load_vendor_presets_function_returns_dicts(tmp_path):
    """load_vendor_presets() directly returns raw dicts, not PresetEntry instances."""
    from bakar.vendor_config import load_vendor_presets

    vendors = tmp_path / "vendors.toml"
    vendors.write_bytes(_NXP_PRESET_TOML.encode())
    result = load_vendor_presets(vendors)
    assert len(result) == 1
    assert isinstance(result[0], dict)
    assert result[0]["name"] == "imx8mp-scarthgap"


def test_load_vendor_presets_missing_file_returns_empty(tmp_path):
    """load_vendor_presets() with nonexistent path returns []."""
    from bakar.vendor_config import load_vendor_presets

    result = load_vendor_presets(tmp_path / "nonexistent.toml")
    assert result == []
