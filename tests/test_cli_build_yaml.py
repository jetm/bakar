"""Tests for the BYO ``varis build my.yml`` surface.

Focuses on argument-parsing logic and overlay materialization. The
real ``kas-container build`` invocation is not exercised here - that
path is covered by the smoke tests in the verification section of the
plan, which require a real workspace.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from varis_build.config import BuildConfig
from varis_build.steps.kas_build import _resolve_user_yaml, materialize_overlay


def _cfg_at(workspace: Path, *, family: str = "nxp") -> BuildConfig:
    return BuildConfig(
        workspace=workspace,
        bsp_family=family,  # type: ignore[arg-type]
        machine="imx95-var-dart",
        distro="fsl-imx-xwayland",
        image="core-image-minimal",
        manifest="imx-6.12.49-2.2.0.xml",
        repo_url="https://example.invalid/repo.git",
        repo_branch="walnascar",
        container_image="jetm/kas-build-env:latest",
    )


def test_kas_yaml_override_resolves_through_property(tmp_path: Path) -> None:
    """When kas_yaml_override is set, cfg.kas_yaml returns it."""
    (tmp_path / "nxp").mkdir()
    user_yaml = tmp_path / "nxp" / "my.yml"
    user_yaml.write_text("machine: imx95-var-dart\n")
    cfg = BuildConfig(
        workspace=tmp_path,
        bsp_family="nxp",
        machine="imx95-var-dart",
        distro="fsl-imx-xwayland",
        image="core-image-minimal",
        manifest="imx-6.12.49-2.2.0.xml",
        repo_url="https://example.invalid/repo.git",
        repo_branch="walnascar",
        container_image="jetm/kas-build-env:latest",
        kas_yaml_override=user_yaml,
    )
    assert cfg.kas_yaml == user_yaml
    assert cfg.default_kas_yaml == tmp_path / "nxp" / "kas-nxp.yml"


def test_kas_yaml_falls_back_to_default(tmp_path: Path) -> None:
    """Without an override, cfg.kas_yaml is the manifest-flow default."""
    cfg = _cfg_at(tmp_path)
    assert cfg.kas_yaml == tmp_path / "nxp" / "kas-nxp.yml"


def test_default_kas_yaml_is_bsp_specific(tmp_path: Path) -> None:
    nxp_cfg = _cfg_at(tmp_path, family="nxp")
    ti_cfg = _cfg_at(tmp_path, family="ti")
    assert nxp_cfg.default_kas_yaml.name == "kas-nxp.yml"
    assert ti_cfg.default_kas_yaml.name == "kas-ti.yml"
    assert nxp_cfg.default_kas_yaml.parent.name == "nxp"
    assert ti_cfg.default_kas_yaml.parent.name == "ti"


def test_resolve_user_yaml_under_bsp_root(tmp_path: Path) -> None:
    """A YAML inside bsp_root is accepted and returned relative."""
    (tmp_path / "nxp").mkdir()
    user_yaml = tmp_path / "nxp" / "my-build.yml"
    user_yaml.write_text("machine: imx95-var-dart\n")
    cfg = _cfg_at(tmp_path)
    rel = _resolve_user_yaml(cfg, user_yaml)
    assert rel == Path("my-build.yml")


def test_resolve_user_yaml_outside_bsp_root_rejects(tmp_path: Path) -> None:
    """A YAML outside bsp_root is rejected with a clear error."""
    (tmp_path / "nxp").mkdir()
    (tmp_path / "varis").mkdir()
    outside_yaml = tmp_path / "varis" / "my-build.yml"
    outside_yaml.write_text("machine: imx95-var-dart\n")
    cfg = _cfg_at(tmp_path)
    with pytest.raises(RuntimeError, match="outside bsp_root"):
        _resolve_user_yaml(cfg, outside_yaml)


def test_materialize_overlay_copies_file(tmp_path: Path) -> None:
    """materialize_overlay copies the source under .varis/overlays/."""
    (tmp_path / "nxp").mkdir()
    overlays_dir = tmp_path / "external-overlays"
    overlays_dir.mkdir()
    overlay_src = overlays_dir / "varis-tuning-nxp.yml"
    overlay_src.write_text("header:\n  version: 3\n")

    cfg = _cfg_at(tmp_path)
    rel = materialize_overlay(cfg, overlay_src)

    assert rel == Path(".varis") / "overlays" / "varis-tuning-nxp.yml"
    dest = cfg.bsp_root / rel
    assert dest.is_file()
    assert not dest.is_symlink()
    assert dest.read_text() == overlay_src.read_text()


def test_materialize_overlay_idempotent(tmp_path: Path) -> None:
    """Calling twice yields the same destination with current content."""
    (tmp_path / "nxp").mkdir()
    overlay_src = tmp_path / "ext" / "varis-tuning-nxp.yml"
    overlay_src.parent.mkdir()
    overlay_src.write_text("header:\n  version: 3\n")

    cfg = _cfg_at(tmp_path)
    rel1 = materialize_overlay(cfg, overlay_src)
    rel2 = materialize_overlay(cfg, overlay_src)
    assert rel1 == rel2

    dest = cfg.bsp_root / rel1
    assert dest.read_text() == overlay_src.read_text()


def test_materialize_overlay_refreshes_content(tmp_path: Path) -> None:
    """Subsequent calls overwrite the destination with the latest source."""
    (tmp_path / "nxp").mkdir()
    overlay_src_a = tmp_path / "a" / "varis-tuning-nxp.yml"
    overlay_src_b = tmp_path / "b" / "varis-tuning-nxp.yml"
    overlay_src_a.parent.mkdir()
    overlay_src_b.parent.mkdir()
    overlay_src_a.write_text("header:\n  version: 3\n# from A\n")
    overlay_src_b.write_text("header:\n  version: 3\n# from B\n")

    cfg = _cfg_at(tmp_path)
    materialize_overlay(cfg, overlay_src_a)
    materialize_overlay(cfg, overlay_src_b)

    dest = cfg.bsp_root / ".varis" / "overlays" / "varis-tuning-nxp.yml"
    assert dest.read_text() == overlay_src_b.read_text()


def test_materialize_overlay_replaces_existing_symlink(tmp_path: Path) -> None:
    """Stale symlinks from earlier varis versions are replaced with copies."""
    (tmp_path / "nxp").mkdir()
    overlay_src = tmp_path / "ext" / "varis-tuning-nxp.yml"
    overlay_src.parent.mkdir()
    overlay_src.write_text("header:\n  version: 3\n")

    cfg = _cfg_at(tmp_path)
    overlay_dir = cfg.bsp_root / ".varis" / "overlays"
    overlay_dir.mkdir(parents=True)
    stale_target = tmp_path / "stale.yml"
    stale_target.write_text("# stale\n")
    (overlay_dir / "varis-tuning-nxp.yml").symlink_to(stale_target)

    materialize_overlay(cfg, overlay_src)

    dest = overlay_dir / "varis-tuning-nxp.yml"
    assert not dest.is_symlink()
    assert dest.read_text() == overlay_src.read_text()


# ---------------------------------------------------------------------------
# Generic mode (BYO without Variscite markers)
# ---------------------------------------------------------------------------


def test_generic_bsp_root_is_yaml_parent(tmp_path: Path) -> None:
    """Generic mode falls back to the YAML's parent dir as bsp_root."""
    pilots = tmp_path / "pilots" / "0005-hardening"
    pilots.mkdir(parents=True)
    user_yaml = pilots / "kas.yml"
    user_yaml.write_text("machine: qemuarm64\n")

    cfg = BuildConfig(
        workspace=tmp_path,
        bsp_family="generic",
        machine="generic",
        distro="generic",
        image="generic",
        manifest="",
        repo_url="",
        repo_branch="",
        container_image="kasproject/kas:latest",
        kas_yaml_override=user_yaml,
    )
    assert cfg.bsp_root == pilots
    assert cfg.kas_yaml == user_yaml


def test_generic_resolve_accepts_minimal_args(tmp_path: Path) -> None:
    """resolve() with bsp_family='generic' fills sensible inert defaults."""
    from varis_build.config import resolve

    pilots = tmp_path / "pilot"
    pilots.mkdir()
    yaml = pilots / "kas.yml"
    yaml.write_text("machine: qemuarm64\n")

    cfg = resolve(workspace=tmp_path, bsp_family="generic", kas_yaml=yaml)

    assert cfg.bsp_family == "generic"
    assert cfg.bsp_root == pilots.resolve()
    assert cfg.kas_yaml == yaml.resolve()
    assert cfg.machine == "generic"
    assert cfg.manifest == ""
    assert cfg.repo_branch == ""


def test_generic_materialize_overlay_under_yaml_parent(tmp_path: Path) -> None:
    """For generic builds the overlay symlink lands next to the user's YAML."""
    pilots = tmp_path / "pilot"
    pilots.mkdir()
    yaml = pilots / "kas.yml"
    yaml.write_text("machine: qemuarm64\n")

    overlay_src = tmp_path / "ext" / "varis-tuning-generic.yml"
    overlay_src.parent.mkdir()
    overlay_src.write_text("header:\n  version: 3\n")

    cfg = BuildConfig(
        workspace=tmp_path,
        bsp_family="generic",
        machine="generic",
        distro="generic",
        image="generic",
        manifest="",
        repo_url="",
        repo_branch="",
        container_image="kasproject/kas:latest",
        kas_yaml_override=yaml,
    )

    rel = materialize_overlay(cfg, overlay_src)

    assert rel == Path(".varis") / "overlays" / "varis-tuning-generic.yml"
    dest = pilots / ".varis" / "overlays" / "varis-tuning-generic.yml"
    assert dest.is_file()
    assert not dest.is_symlink()
    assert dest.read_text() == overlay_src.read_text()


def test_generic_resolve_user_yaml_relative_to_yaml_parent(tmp_path: Path) -> None:
    """The user's YAML resolves to a flat filename in generic mode."""
    pilots = tmp_path / "pilot"
    pilots.mkdir()
    yaml = pilots / "kas.yml"
    yaml.write_text("machine: qemuarm64\n")

    cfg = BuildConfig(
        workspace=tmp_path,
        bsp_family="generic",
        machine="generic",
        distro="generic",
        image="generic",
        manifest="",
        repo_url="",
        repo_branch="",
        container_image="kasproject/kas:latest",
        kas_yaml_override=yaml,
    )

    rel = _resolve_user_yaml(cfg, yaml)
    assert rel == Path("kas.yml")
