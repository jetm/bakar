"""Property-based tests for bakar.config resolution and branch inference."""

from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from bakar.config import BuildConfig, infer_repo_branch, resolve
from bakar.workspace_config import WorkspaceConfig

# Manifest values fed to resolve land in env vars and dataclass fields; exclude
# control characters (notably embedded null bytes) that os.environ rejects.
_MANIFEST_TEXT = st.text(
    alphabet=st.characters(min_codepoint=33, max_codepoint=126),
    min_size=1,
)


@pytest.mark.unit
@given(manifest=st.text(min_size=1))
def test_infer_repo_branch_never_raises_and_returns_nonempty(manifest: str) -> None:
    """infer_repo_branch always returns a non-empty string and never raises."""
    result = infer_repo_branch(manifest, fallback="main")
    assert isinstance(result, str)
    assert result != ""


@pytest.mark.unit
@given(manifest=st.from_regex(r"imx-6\.6\.[^\s]+\.xml", fullmatch=True))
def test_infer_repo_branch_nxp_66_is_scarthgap(manifest: str) -> None:
    """NXP-manifest-shaped strings for the 6.6 line always map to scarthgap."""
    assert infer_repo_branch(manifest) == "scarthgap"


@pytest.mark.unit
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    explicit_manifest=_MANIFEST_TEXT,
    env_manifest=_MANIFEST_TEXT,
)
def test_resolve_explicit_manifest_beats_env(
    monkeypatch: pytest.MonkeyPatch,
    explicit_manifest: str,
    env_manifest: str,
) -> None:
    """An explicit manifest arg wins over BAKAR_MANIFEST env var."""
    monkeypatch.setenv("BAKAR_MANIFEST", env_manifest)
    cfg = resolve(
        workspace=Path("/tmp/ws"),
        bsp_family="nxp",
        manifest=explicit_manifest,
        workspace_config=WorkspaceConfig(),
    )
    assert isinstance(cfg, BuildConfig)
    assert cfg.manifest == explicit_manifest


@pytest.mark.unit
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(env_manifest=_MANIFEST_TEXT)
def test_resolve_env_manifest_used_when_no_explicit_arg(
    monkeypatch: pytest.MonkeyPatch,
    env_manifest: str,
) -> None:
    """With no explicit manifest arg, BAKAR_MANIFEST env var is used."""
    monkeypatch.setenv("BAKAR_MANIFEST", env_manifest)
    cfg = resolve(workspace=Path("/tmp/ws"), bsp_family="nxp", workspace_config=WorkspaceConfig())
    assert cfg.manifest == env_manifest
