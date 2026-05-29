"""Property-based tests for the pure BSP classifier functions.

Encodes two classes of property:

1. Totality - the classifier never raises and always returns a value from its
   documented output set, for any input the strategy can generate.
2. Targeted shape - inputs matching a documented manifest/machine pattern map to
   the expected specific label.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from bakar.bsp_detect import detect_bsp_from_yaml
from bakar.bsp_model import detect_bsp_family

# detect_bsp_family (bsp_model) classifies by manifest filename: Literal["nxp", "ti", "unknown"].
_FAMILY_OUTPUTS = {"nxp", "ti", "unknown"}
# detect_bsp_from_yaml (bsp_detect) classifies a kas YAML by content.
_YAML_OUTPUTS = {"nxp", "ti", "generic", "unknown"}


@pytest.mark.unit
@given(st.text())
def test_detect_bsp_family_totality(name: str) -> None:
    """For any string the result is in the allowed set and never raises."""
    assert detect_bsp_family(Path(name)) in _FAMILY_OUTPUTS


@pytest.mark.unit
@given(st.from_regex(r"imx-\d+\.\d+\.\d+-\d+\.\d+\.\d+\.xml", fullmatch=True))
def test_detect_bsp_family_nxp_shape(name: str) -> None:
    """An NXP-shaped manifest filename always classifies as nxp."""
    assert detect_bsp_family(Path(name)) == "nxp"


# The TI processor-sdk regex in bsp_model requires the full structured form:
# processor-sdk-<poky>-<flavour>-<4-part-sdk>-config_var<N>.txt. A loose
# `processor-sdk-*.txt` (e.g. processor-sdk-0.txt) classifies as unknown, so the
# name is assembled from components that mirror _TI_PROCESSOR_SDK_RE.
@pytest.mark.unit
@given(
    poky=st.from_regex(r"[A-Za-z][a-z]+", fullmatch=True),
    flavour=st.from_regex(r"[a-z][a-z\-]*[a-z]", fullmatch=True),
    sdk=st.from_regex(r"\d+\.\d+\.\d+\.\d+", fullmatch=True),
    var=st.from_regex(r"\d+", fullmatch=True),
)
def test_detect_bsp_family_ti_shape(poky: str, flavour: str, sdk: str, var: str) -> None:
    """A TI-shaped processor-sdk config filename always classifies as ti."""
    name = f"processor-sdk-{poky}-{flavour}-{sdk}-config_var{var}.txt"
    assert detect_bsp_family(Path(name)) == "ti"


@pytest.mark.unit
@given(st.text())
def test_detect_bsp_from_yaml_totality_missing_path(name: str) -> None:
    """A path that does not exist must not raise and falls back to unknown."""
    assert detect_bsp_from_yaml(Path("/nonexistent-bakar-prop") / (name or "x")) in _YAML_OUTPUTS


@pytest.mark.unit
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(machine=st.text())
def test_detect_bsp_from_yaml_totality_real_file(tmp_path, machine: str) -> None:
    """For any machine string in a valid YAML file the result is in the allowed set."""
    p = tmp_path / "kas.yml"
    p.write_text(yaml.safe_dump({"machine": machine}), encoding="utf-8")
    assert detect_bsp_from_yaml(p) in _YAML_OUTPUTS


@pytest.mark.unit
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.just("imx8mp-var-dart"))
def test_detect_bsp_from_yaml_nxp_machine(tmp_path, machine: str) -> None:
    """A machine starting with imx classifies the YAML as nxp."""
    p = tmp_path / "kas.yml"
    p.write_text(f"machine: {machine}\n", encoding="utf-8")
    assert detect_bsp_from_yaml(p) == "nxp"


@pytest.mark.unit
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.just("am62x"))
def test_detect_bsp_from_yaml_ti_machine(tmp_path, machine: str) -> None:
    """A machine starting with am classifies the YAML as ti."""
    p = tmp_path / "kas.yml"
    p.write_text(f"machine: {machine}\n", encoding="utf-8")
    assert detect_bsp_from_yaml(p) == "ti"
