"""Unit tests for varis_build.bsp_model.infer_bsp_branch."""

from __future__ import annotations

import pytest

from varis_build.bsp_model import infer_bsp_branch


@pytest.mark.parametrize(
    "config,expected",
    [
        (
            "processor-sdk-scarthgap-chromium-11.00.09.04-config_var01.txt",
            "scarthgap_11.00.09.04_var01",
        ),
        (
            "processor-sdk-scarthgap-non-chromium-11.00.09.04-config_var02.txt",
            "scarthgap_11.00.09.04_var02",
        ),
        (
            "processor-sdk-walnascar-chromium-12.00.00.01-config_var03.txt",
            "walnascar_12.00.00.01_var03",
        ),
    ],
)
def test_infer_canonical(config: str, expected: str) -> None:
    assert infer_bsp_branch(config) == expected


@pytest.mark.parametrize(
    "config",
    [
        "garbage.txt",
        "arago-scarthgap-config.txt",  # legacy shape, distinct branch scheme
        "imx-6.6.52-2.2.2.xml",  # NXP filename
        "",
        "processor-sdk-08.02.00.24-config_var01.txt",  # pre-poky-name shape
        "processor-sdk-scarthgap-11.00.09.04-config_var01.txt",  # missing flavour token
    ],
)
def test_infer_malformed_returns_unknown(config: str) -> None:
    assert infer_bsp_branch(config) == "<unknown>"
