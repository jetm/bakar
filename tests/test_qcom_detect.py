"""Unit tests for Qualcomm QLI manifest detection in bakar.bsp_model.

Pins the rule that ``detect_bsp_family`` classifies a ``qcom-*.xml``
manifest filename as the ``qcom`` family, checked after vendor entries
and alongside the built-in NXP/TI regexes. Pure filename inspection -
no I/O, so the file need not exist on disk.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bakar.bsp_model import detect_bsp_family

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "filename",
    [
        "qcom-6.6.119-QLI.1.8-Ver.1.1_qim-product-sdk-2.3.1.xml",
        "qcom-6.6.119.xml",
        "qcom-anything.xml",
    ],
)
def test_detect_qcom_manifest(filename: str) -> None:
    assert detect_bsp_family(Path(filename)) == "qcom"


@pytest.mark.parametrize(
    "filename",
    [
        "imx-6.6.52-2.2.2.xml",
        "imx-6.12.49-2.2.0.xml",
    ],
)
def test_qcom_regex_does_not_misclassify_nxp(filename: str) -> None:
    assert detect_bsp_family(Path(filename)) == "nxp"


@pytest.mark.parametrize(
    "filename",
    [
        "processor-sdk-scarthgap-chromium-11.00.09.04-config_var01.txt",
        "arago-scarthgap-config.txt",
    ],
)
def test_qcom_regex_does_not_misclassify_ti(filename: str) -> None:
    assert detect_bsp_family(Path(filename)) == "ti"
