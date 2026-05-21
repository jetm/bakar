"""Unit tests for bspctl.kas YAML generation.

The generator is now topology-only: machine, distro, target, and
repos. The BSP tuning block (``local_conf_header``) and the
meta-varis-overrides repo entry live in the static overlay YAMLs at
``overlays/bspctl-tuning-<bsp>.yml`` and are layered on top by ``bspctl
build`` at run time. These tests pin that contract so a regression
that re-injects either piece into the generator output is caught
immediately.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from bspctl.kas import (
    NXP_KAS_TEMPLATE,
    TI_KAS_TEMPLATE,
    KasGenOptions,
    KasTemplate,
    build_yaml_dict,
)

if TYPE_CHECKING:
    from pathlib import Path


def _make_options(
    workspace: Path,
    template: KasTemplate,
    *,
    bblayers: Path | None = None,
) -> KasGenOptions:
    return KasGenOptions(
        manifest=workspace / "fake-manifest.txt",
        bblayers=bblayers,
        machine="am62x-var-som",
        distro="arago",
        target="var-thin-image",
        output=workspace / "kas-out.yml",
        workspace=workspace,
        template=template,
        skip_manifest=True,
    )


def test_template_is_workspace_subdir_only() -> None:
    """KasTemplate now carries only workspace_subdir; tuning lives in the overlay."""
    assert NXP_KAS_TEMPLATE.workspace_subdir == "nxp"
    assert TI_KAS_TEMPLATE.workspace_subdir == "ti"


def test_topology_output_has_no_local_conf_header(tmp_path: Path) -> None:
    """The generator must not emit a local_conf_header block.

    The tuning lives in overlays/bspctl-tuning-<bsp>.yml; layering it
    in at build time is what keeps BYO and manifest flows in sync.
    """
    (tmp_path / "nxp").mkdir()
    out = build_yaml_dict(_make_options(tmp_path, NXP_KAS_TEMPLATE))
    assert "local_conf_header" not in out


def test_topology_output_has_no_meta_varis_overrides_for_nxp(tmp_path: Path) -> None:
    """meta-varis-overrides is part of the optimization stack, not topology."""
    (tmp_path / "nxp").mkdir()
    out = build_yaml_dict(_make_options(tmp_path, NXP_KAS_TEMPLATE))
    assert "meta-varis-overrides" not in out["repos"]


def test_topology_output_has_no_meta_varis_overrides_for_ti(tmp_path: Path) -> None:
    """meta-varis-overrides-ti is part of the optimization stack, not topology."""
    (tmp_path / "ti").mkdir()
    out = build_yaml_dict(_make_options(tmp_path, TI_KAS_TEMPLATE))
    assert "meta-varis-overrides-ti" not in out["repos"]


def test_topology_output_has_no_overrides_when_dir_present(tmp_path: Path) -> None:
    """Even when meta-varis-overrides/conf/layer.conf exists on disk, the
    generator must not include it - the overlay carries that repo entry now."""
    overrides = tmp_path / "nxp" / "meta-varis-overrides"
    overrides.mkdir(parents=True)
    (overrides / "conf").mkdir()
    (overrides / "conf" / "layer.conf").write_text('BBFILE_COLLECTIONS += "meta-varis-overrides"\n')
    out = build_yaml_dict(_make_options(tmp_path, NXP_KAS_TEMPLATE))
    assert "meta-varis-overrides" not in out["repos"]


def test_topology_output_keeps_machine_distro_target(tmp_path: Path) -> None:
    """Pin the topology fields the generator is responsible for."""
    (tmp_path / "ti").mkdir()
    out = build_yaml_dict(_make_options(tmp_path, TI_KAS_TEMPLATE))
    assert out["machine"] == "am62x-var-som"
    assert out["distro"] == "arago"
    assert out["target"] == "var-thin-image"
    assert out["header"] == {"version": 3}
