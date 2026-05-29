"""Property-based tests for bakar.bsp_model."""

from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from bakar.bsp_model import BspModel, detect_bsp_family, get_model

# detect_bsp_family inspects only ``path.name`` and returns one of these for
# built-in classification; a vendor overlay could in principle return its own
# family string, but absent vendor config the result is always in this set.
KNOWN_FAMILIES = {"nxp", "ti", "unknown"}


@pytest.mark.unit
@given(name=st.text())
@settings(suppress_health_check=[HealthCheck.too_slow])
def test_detect_bsp_family_returns_known_member(name: str) -> None:
    """detect_bsp_family always returns a known family and never raises."""
    assert detect_bsp_family(Path(name)) in KNOWN_FAMILIES


@pytest.mark.unit
@given(name=st.text())
@settings(suppress_health_check=[HealthCheck.too_slow])
def test_detect_bsp_family_none_is_unknown(name: str) -> None:
    """A None manifest path with no config classifies as unknown."""
    assert detect_bsp_family(None) == "unknown"


@pytest.mark.unit
def test_get_model_nxp_well_formed() -> None:
    """get_model("nxp") returns a BspModel with non-empty defaults."""
    model = get_model("nxp")
    assert isinstance(model, BspModel)
    assert model.default_machine
    assert model.default_manifest


@pytest.mark.unit
def test_get_model_ti_well_formed() -> None:
    """get_model("ti") returns a BspModel with non-empty defaults."""
    model = get_model("ti")
    assert isinstance(model, BspModel)
    assert model.default_machine
    assert model.default_manifest
