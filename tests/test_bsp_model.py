"""Unit tests for bspctl.bsp_model.

Covers detection (NXP / TI / unknown manifest filenames) and the
``get_model`` factory; both are pure data lookups so the tests do not
need a real workspace.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from bspctl.bsp_model import BspModel, detect_bsp_family, get_model

# ---------------------------------------------------------------------------
# detect_bsp_family
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "filename",
    [
        "imx-6.6.52-2.2.2.xml",
        "imx-6.12.49-2.2.0.xml",
        "imx-6.6.23-2.0.0.xml",
        "imx-6.12.20-2.0.0.xml",
    ],
)
def test_detect_nxp_manifest(filename: str) -> None:
    assert detect_bsp_family(Path(filename)) == "nxp"


@pytest.mark.parametrize(
    "filename",
    [
        "processor-sdk-scarthgap-chromium-11.00.09.04-config_var01.txt",
        "processor-sdk-scarthgap-non-chromium-11.00.09.04-config_var02.txt",
        "processor-sdk-walnascar-chromium-12.00.00.01-config_var01.txt",
    ],
)
def test_detect_ti_processor_sdk(filename: str) -> None:
    assert detect_bsp_family(Path(filename)) == "ti"


@pytest.mark.parametrize(
    "filename",
    [
        "arago-scarthgap-config.txt",
        "arago-scarthgap-11x-config.txt",
        "arago-scarthgap-next-config.txt",
        "arago-dunfell-config.txt",
    ],
)
def test_detect_ti_arago_legacy(filename: str) -> None:
    assert detect_bsp_family(Path(filename)) == "ti"


@pytest.mark.parametrize(
    "filename",
    [
        "garbage.xml",
        "imx-6.6.52.xml",  # missing -X.Y.Z BSP version
        "manifest.xml",
        "processor-sdk-08.02.00.24-config_var01.txt",  # legacy 4-digit-only, no <poky>
        "config.txt",
        "",
        "imx-foo-bar.xml",
        "processor-sdk-scarthgap-chromium-11-config_var01.txt",  # SDK truncated
    ],
)
def test_detect_unknown(filename: str) -> None:
    assert detect_bsp_family(Path(filename)) == "unknown"


def test_detect_none_input() -> None:
    """Passing None for both args returns 'unknown' rather than raising."""
    assert detect_bsp_family(None, None) == "unknown"


def test_detect_config_fallback(tmp_path: Path) -> None:
    """A config file containing 'meta-variscite-bsp-ti' classifies as TI
    even when the manifest filename is not a recognized shape."""
    cfg = tmp_path / "bblayers.conf"
    cfg.write_text('BBLAYERS += " /work/sources/meta-variscite-bsp-ti "\n')
    assert detect_bsp_family(Path("garbage.xml"), config_file=cfg) == "ti"


def test_vendor_registry_takes_precedence_over_builtin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A user vendor entry matches before the built-in NXP regex.

    The manifest 'myco-1.0.xml' does not match the built-in NXP pattern
    (imx-A.B.C-X.Y.Z.xml) but matches the user's regex. The vendor entry
    must win and return its declared family.
    """
    from bspctl.vendor_config import VendorEntry

    vendor = VendorEntry(name="myco", family="nxp", manifest_regex=r"^myco-.*\.xml$")
    monkeypatch.setattr("bspctl.bsp_model.load_vendors", lambda: [vendor])

    assert detect_bsp_family(Path("myco-1.0.xml")) == "nxp"


# ---------------------------------------------------------------------------
# get_model
# ---------------------------------------------------------------------------


def test_get_model_nxp() -> None:
    bsp = get_model("nxp")
    assert isinstance(bsp, BspModel)
    assert bsp.family == "nxp"
    assert bsp.workspace_subdir == "nxp"
    assert bsp.kas_yaml_filename == "kas-nxp.yml"
    assert bsp.tuning_overlay_filename == "bspctl-tuning-nxp.yml"
    assert bsp.manifest_kind == "repo-xml"
    assert bsp.default_machine == "imx8mp-var-dart"
    assert bsp.default_distro == "fsl-imx-xwayland"
    assert bsp.default_image == "core-image-minimal"
    assert "repo" in bsp.required_host_tools
    assert "git" not in bsp.required_host_tools  # repo is the NXP gate
    assert bsp.kas_template.workspace_subdir == "nxp"
    # NXP doctor extras should include the linux-imx fork check
    extras = {fn.__name__ for fn in bsp.doctor_extras}
    assert "check_forks_linux_imx" in extras
    assert "check_manifest_consistency" in extras
    assert "check_git_object_cache" in extras


def test_get_model_ti() -> None:
    bsp = get_model("ti")
    assert isinstance(bsp, BspModel)
    assert bsp.family == "ti"
    assert bsp.workspace_subdir == "ti"
    assert bsp.kas_yaml_filename == "kas-ti.yml"
    assert bsp.tuning_overlay_filename == "bspctl-tuning-ti.yml"
    assert bsp.manifest_kind == "oe-layertool-config"
    assert bsp.default_machine == "am62x-var-som"
    assert bsp.default_distro == "arago"
    assert bsp.default_image == "var-thin-image"
    assert "git" in bsp.required_host_tools
    assert "repo" not in bsp.required_host_tools  # TI does not use repo-tool
    assert bsp.kas_template.workspace_subdir == "ti"
    # TI doctor extras should include the four ti_* checks
    extras = {fn.__name__ for fn in bsp.doctor_extras}
    assert "check_ti_layertool_present" in extras
    assert "check_ti_layertool_config_consistency" in extras
    assert "check_forks_ti_linux_kernel" in extras
    assert "check_forks_ti_u_boot" in extras


def test_get_model_unknown_raises() -> None:
    with pytest.raises(ValueError, match="Unknown BSP family"):
        get_model("openbsd")  # type: ignore[arg-type]


def test_get_model_applies_vendor_override() -> None:
    """A matching VendorEntry overrides only its non-None optional fields.

    The vendor sets only default_machine; every other BspModel field must
    remain equal to the built-in NXP preset value.
    """
    from bspctl.vendor_config import VendorEntry

    vendor = VendorEntry(
        name="myco",
        family="nxp",
        manifest_regex=r"^myco-.*\.xml$",
        default_machine="my-board",
    )

    with patch("bspctl.bsp_model.load_vendors", return_value=[vendor]):
        model = get_model("nxp")

    assert model.default_machine == "my-board"

    # All other fields must match the built-in NXP preset.
    assert model.family == "nxp"
    assert model.workspace_subdir == "nxp"
    assert model.kas_yaml_filename == "kas-nxp.yml"
    assert model.tuning_overlay_filename == "bspctl-tuning-nxp.yml"
    assert model.manifest_kind == "repo-xml"
    assert model.default_distro == "fsl-imx-xwayland"
    assert model.default_image == "core-image-minimal"
