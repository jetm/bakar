"""Sanity checks on the static tuning overlay YAMLs.

The overlays carry every optimization that used to live in the
generator's ``local_conf_header`` block. These tests parse the shipped
files and assert each load-bearing line is present, so a regression
that drops e.g. the renderdoc fix or the BB_FETCH_TIMEOUT bump fails
in CI before it lands in a build.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
NXP_OVERLAY = REPO_ROOT / "overlays" / "bspctl-tuning-nxp.yml"
TI_OVERLAY = REPO_ROOT / "overlays" / "bspctl-tuning-ti.yml"
GENERIC_OVERLAY = REPO_ROOT / "overlays" / "bspctl-tuning-generic.yml"

_SHARED_LINES = (
    'CCACHE_DIR = "/work/ccache"',
    'INHERIT += "ccache"',
    "BB_NUMBER_THREADS",
    "PARALLEL_MAKE",
    "IMAGE_FEATURES:remove",
    'BB_FETCH_TIMEOUT = "600"',
    "MIRRORS = ",
    "PREMIRRORS:prepend = ",
    "FETCHCMD_wget",
)

_NXP_ONLY_LINES = (
    'ACCEPT_FSL_EULA = "1"',
    'CMAKE_CXX_COMPILER_LAUNCHER:pn-renderdoc = ""',
    'CMAKE_C_COMPILER_LAUNCHER:pn-renderdoc = ""',
    "varigit/linux-imx",
    "/work/forks/linux-imx",
)


def _load(path: Path) -> dict:
    assert path.is_file(), f"overlay missing: {path}"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.fixture
def nxp_overlay() -> dict:
    return _load(NXP_OVERLAY)


@pytest.fixture
def ti_overlay() -> dict:
    return _load(TI_OVERLAY)


@pytest.fixture
def generic_overlay() -> dict:
    return _load(GENERIC_OVERLAY)


def test_nxp_overlay_has_kas_header(nxp_overlay: dict) -> None:
    assert nxp_overlay.get("header") == {"version": 3}


def test_ti_overlay_has_kas_header(ti_overlay: dict) -> None:
    assert ti_overlay.get("header") == {"version": 3}


def test_nxp_overlay_carries_shared_tuning(nxp_overlay: dict) -> None:
    body = nxp_overlay["local_conf_header"]["bspctl-tuning"]
    for needle in _SHARED_LINES:
        assert needle in body, f"NXP overlay missing: {needle!r}"


def test_nxp_overlay_carries_nxp_only_tuning(nxp_overlay: dict) -> None:
    body = nxp_overlay["local_conf_header"]["bspctl-tuning"]
    for needle in _NXP_ONLY_LINES:
        assert needle in body, f"NXP overlay missing: {needle!r}"


def test_ti_overlay_carries_shared_tuning(ti_overlay: dict) -> None:
    body = ti_overlay["local_conf_header"]["bspctl-tuning"]
    for needle in _SHARED_LINES:
        assert needle in body, f"TI overlay missing: {needle!r}"


def test_ti_overlay_omits_nxp_specific_knobs(ti_overlay: dict) -> None:
    """ACCEPT_FSL_EULA and renderdoc are NXP-specific."""
    body = ti_overlay["local_conf_header"]["bspctl-tuning"]
    assert "ACCEPT_FSL_EULA" not in body
    assert "renderdoc" not in body


def test_ti_overlay_carries_ti_fork_premirrors(ti_overlay: dict) -> None:
    body = ti_overlay["local_conf_header"]["bspctl-tuning"]
    assert "/work/forks/ti-linux-kernel" in body
    assert "/work/forks/ti-u-boot" in body


def test_nxp_overlay_carries_meta_varis_overrides(nxp_overlay: dict) -> None:
    """The override layer ships with the NXP overlay, not the generator output."""
    repos = nxp_overlay.get("repos") or {}
    assert "meta-varis-overrides" in repos
    assert repos["meta-varis-overrides"]["path"] == "meta-varis-overrides"


def test_ti_overlay_carries_meta_varis_overrides_ti(ti_overlay: dict) -> None:
    repos = ti_overlay.get("repos") or {}
    assert "meta-varis-overrides-ti" in repos
    assert repos["meta-varis-overrides-ti"]["path"] == "meta-varis-overrides-ti"


def test_generic_overlay_has_kas_header(generic_overlay: dict) -> None:
    assert generic_overlay.get("header") == {"version": 3}


def test_generic_overlay_carries_shared_tuning(generic_overlay: dict) -> None:
    body = generic_overlay["local_conf_header"]["bspctl-tuning"]
    for needle in _SHARED_LINES:
        assert needle in body, f"generic overlay missing: {needle!r}"


def test_generic_overlay_omits_nxp_specific_knobs(generic_overlay: dict) -> None:
    """The generic overlay must not pull in NXP-only knobs."""
    body = generic_overlay["local_conf_header"]["bspctl-tuning"]
    assert "ACCEPT_FSL_EULA" not in body
    assert "renderdoc" not in body
    assert "linux-imx" not in body


def test_generic_overlay_omits_ti_specific_knobs(generic_overlay: dict) -> None:
    body = generic_overlay["local_conf_header"]["bspctl-tuning"]
    assert "ti-linux-kernel" not in body
    assert "ti-u-boot" not in body


def test_generic_overlay_omits_meta_varis_overrides(generic_overlay: dict) -> None:
    """The vendor carry layer is irrelevant for non-NXP/TI builds."""
    repos = generic_overlay.get("repos") or {}
    assert "meta-varis-overrides" not in repos
    assert "meta-varis-overrides-ti" not in repos


def test_generic_overlay_declares_pythonmalloc_env(generic_overlay: dict) -> None:
    """PYTHONMALLOC=malloc is BSP-agnostic; the parser fork race fires on every BSP."""
    assert generic_overlay.get("env") == {"PYTHONMALLOC": "malloc"}
