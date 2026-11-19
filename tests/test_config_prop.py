"""Property-based tests for bakar.config resolution and branch inference."""

from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from bakar.config import BSPSpec, BuildConfig, ResolveRequest, infer_repo_branch, resolve
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
        ResolveRequest(
            workspace=Path("/tmp/ws"),
            bsp_family="nxp",
            spec=BSPSpec(manifest=explicit_manifest),
            workspace_config=WorkspaceConfig(),
        )
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
    cfg = resolve(ResolveRequest(workspace=Path("/tmp/ws"), bsp_family="nxp", workspace_config=WorkspaceConfig()))
    assert cfg.manifest == env_manifest


def _meta_avocado_cfg(stem: str, *, host_mode: bool = True) -> BuildConfig:
    """A meta-avocado generic BuildConfig whose bsp_root is ``build-<stem>``.

    meta-avocado classifies as the ``generic`` family with machine degenerating
    to the literal ``"generic"``; ``bsp_root.name`` is the disambiguator, so two
    stems produce two distinct roots under one workspace.
    """
    workspace = Path("/ws/sources")
    return BuildConfig(
        workspace=workspace,
        bsp_family="generic",
        machine="generic",
        distro="poky",
        image="core-image-minimal",
        manifest="",
        repo_url="",
        repo_branch="scarthgap",
        kas_container_image="",
        host_mode=host_mode,
        kas_yaml_override=workspace / "meta-avocado" / "kas" / "machine" / f"{stem}.yml",
    )


@pytest.mark.unit
def test_resolved_tmpdir_default_is_workspace_relative_when_unset() -> None:
    """Default -> ``bsp_root/build_dir_name/tmp`` with no doubled ``build`` segment."""
    cfg = _meta_avocado_cfg("qemux86-64")
    assert cfg.resolved_tmpdir == cfg.bsp_root / cfg.build_dir_name / "tmp"
    assert cfg.resolved_tmpdir == Path("/ws/sources/build-qemux86-64/build/tmp")


@pytest.mark.unit
def test_resolved_tmpdir_ignores_env_tmpdir(monkeypatch: pytest.MonkeyPatch) -> None:
    """An env ``TMPDIR`` never leaks into the resolved path (unlike SSTATE_DIR)."""
    monkeypatch.setenv("TMPDIR", "/run/user/1000/tmp")
    unset = _meta_avocado_cfg("qemux86-64")
    assert unset.resolved_tmpdir == Path("/ws/sources/build-qemux86-64/build/tmp")
    assert "/run/user/1000/tmp" not in str(unset.resolved_tmpdir)


@pytest.mark.unit
def test_resolved_tmpdir_distinct_per_bsp_root() -> None:
    """Two meta-avocado stems sharing machine "generic" get distinct tmp paths."""
    a = _meta_avocado_cfg("qemux86-64")
    b = _meta_avocado_cfg("qemuarm64")
    assert a.machine == b.machine == "generic"
    assert a.resolved_tmpdir != b.resolved_tmpdir


@pytest.mark.unit
def test_resolved_tmpdir_qcom_uses_tmp_glibc_leaf() -> None:
    """The default leaf is ``tmp-glibc`` for qcom, ``tmp`` elsewhere."""
    qcom = BuildConfig(
        workspace=Path("/ws"),
        bsp_family="qcom",
        machine="qcs6490-rb3gen2-core-kit",
        distro="qcom-wayland",
        image="core-image-minimal",
        manifest="",
        repo_url="",
        repo_branch="scarthgap",
        kas_container_image="",
    )
    assert qcom.resolved_tmpdir == qcom.bsp_root / qcom.build_dir_name / "tmp-glibc"
    assert qcom.resolved_tmpdir.name == "tmp-glibc"


@pytest.mark.unit
def test_resolved_tmpdir_distinct_across_nxp_workspaces() -> None:
    """Two nxp checkouts (same family dir, same machine) never share one tmp."""

    def _nxp(workspace: str) -> BuildConfig:
        return BuildConfig(
            workspace=Path(workspace),
            bsp_family="nxp",
            machine="imx8mp-var-dart",
            distro="fsl-imx-wayland",
            image="core-image-minimal",
            manifest="",
            repo_url="",
            repo_branch="scarthgap",
            kas_container_image="",
            host_mode=True,
        )

    a = _nxp("/work-a")
    b = _nxp("/work-b")
    assert a.bsp_root.name == b.bsp_root.name == "nxp"
    assert a.machine == b.machine
    assert a.resolved_tmpdir != b.resolved_tmpdir


@pytest.mark.unit
def test_resolved_tmpdir_distinct_across_qcom_distros() -> None:
    """Two qcom distros in one checkout, same machine, never share one tmp.

    qcom ``bsp_root`` is ``workspace/qcom`` for every distro; only
    ``build_dir_name`` (``build-<distro>``) varies.
    """

    def _qcom(distro: str) -> BuildConfig:
        return BuildConfig(
            workspace=Path("/ws"),
            bsp_family="qcom",
            machine="qcs6490-rb3gen2-core-kit",
            distro=distro,
            image="core-image-minimal",
            manifest="",
            repo_url="",
            repo_branch="scarthgap",
            kas_container_image="",
            host_mode=True,
        )

    a = _qcom("qcom-wayland")
    b = _qcom("qcom-x11")
    assert a.bsp_root == b.bsp_root  # both workspace/qcom
    assert a.machine == b.machine
    assert a.build_dir_name != b.build_dir_name  # build-qcom-wayland vs build-qcom-x11
    assert a.resolved_tmpdir != b.resolved_tmpdir
