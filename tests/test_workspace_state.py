"""Unit tests for bakar.workspace.WorkspaceState properties.

Pins the bsp_family-scoped behavior of `repo_broken` / `needs_full_reinit`:
TI has no `.repo/` manifest semantics, so its manifest-include /
manifests-branch fields are intentionally None and must not be
interpreted as repo corruption. Without this scoping, every TI
`bakar build` would force_init=True, which makes
``oe-layertool-setup.sh -r`` reset every checkout under
``ti/sources/`` - clobbering any manual override (e.g. swapping
the BSP-bundled bitbake for a local fork).
"""

from __future__ import annotations

import pytest

from bakar.workspace import WorkspaceState

pytestmark = pytest.mark.unit


def _ti_state(*, repo_initialized: bool = True) -> WorkspaceState:
    return WorkspaceState(
        bsp_family="ti",
        repo_initialized=repo_initialized,
        sources_populated=True,
        build_dir_exists=True,
        bblayers_present=True,
        kas_yaml_present=True,
        forks_linux_imx=False,
        cache_dirs_ok=True,
        repo_manifest_include=None,
        repo_manifests_branch=None,
        requested_manifest="processor-sdk-scarthgap-chromium-11.00.09.04-config_var01.txt",
        requested_branch="scarthgap_11.00.09.04_var01",
    )


def _nxp_state(*, manifest_include: str | None = "imx-6.6.52-2.2.2.xml") -> WorkspaceState:
    return WorkspaceState(
        bsp_family="nxp",
        repo_initialized=True,
        sources_populated=True,
        build_dir_exists=True,
        bblayers_present=True,
        kas_yaml_present=True,
        forks_linux_imx=True,
        cache_dirs_ok=True,
        repo_manifest_include=manifest_include,
        repo_manifests_branch="scarthgap",
        requested_manifest="imx-6.6.52-2.2.2.xml",
        requested_branch="scarthgap",
    )


def test_repo_broken_false_on_ti_even_with_no_manifest_include() -> None:
    state = _ti_state()
    assert state.repo_broken is False
    assert state.needs_full_reinit is False


def test_repo_broken_false_on_ti_with_uninitialized_repo() -> None:
    state = _ti_state(repo_initialized=False)
    assert state.repo_broken is False
    assert state.needs_full_reinit is False


def test_repo_broken_true_on_nxp_when_manifest_include_missing() -> None:
    state = _nxp_state(manifest_include=None)
    assert state.repo_broken is True
    assert state.needs_full_reinit is True


def test_repo_broken_false_on_nxp_when_manifest_include_present() -> None:
    state = _nxp_state(manifest_include="imx-6.6.52-2.2.2.xml")
    assert state.repo_broken is False
    assert state.needs_full_reinit is False
