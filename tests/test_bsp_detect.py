"""Unit tests for bspctl.bsp_detect.detect_bsp_from_yaml.

Pins the rules that classify a kas YAML as NXP, TI, generic, or
unknown for the BYO ``varis build my.yml`` flow. Order: machine
prefix wins, then repos block names, then a generic fallback for
parseable YAMLs that have at least one of those anchors.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from bspctl.bsp_detect import detect_bsp_from_yaml, detect_kas_workspace, is_meta_avocado_yaml

if TYPE_CHECKING:
    from pathlib import Path


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "kas.yml"
    path.write_text(body, encoding="utf-8")
    return path


def test_machine_imx_classifies_as_nxp(tmp_path: Path) -> None:
    p = _write(tmp_path, "machine: imx95-var-dart\nrepos: {}\n")
    assert detect_bsp_from_yaml(p) == "nxp"


def test_machine_imx8mp_classifies_as_nxp(tmp_path: Path) -> None:
    p = _write(tmp_path, "machine: imx8mp-var-dart\nrepos: {}\n")
    assert detect_bsp_from_yaml(p) == "nxp"


def test_machine_am62x_classifies_as_ti(tmp_path: Path) -> None:
    p = _write(tmp_path, "machine: am62x-var-som\nrepos: {}\n")
    assert detect_bsp_from_yaml(p) == "ti"


def test_machine_k3_classifies_as_ti(tmp_path: Path) -> None:
    p = _write(tmp_path, "machine: k3-am625-evm\nrepos: {}\n")
    assert detect_bsp_from_yaml(p) == "ti"


def test_repos_meta_imx_classifies_as_nxp_when_machine_missing(tmp_path: Path) -> None:
    body = "repos:\n  meta-imx:\n    path: sources/meta-imx\n"
    p = _write(tmp_path, body)
    assert detect_bsp_from_yaml(p) == "nxp"


def test_repos_meta_freescale_classifies_as_nxp(tmp_path: Path) -> None:
    body = "repos:\n  meta-freescale:\n    path: sources/meta-freescale\n"
    p = _write(tmp_path, body)
    assert detect_bsp_from_yaml(p) == "nxp"


def test_repos_meta_ti_bsp_classifies_as_ti(tmp_path: Path) -> None:
    body = "repos:\n  meta-ti-bsp:\n    path: sources/meta-ti-bsp\n"
    p = _write(tmp_path, body)
    assert detect_bsp_from_yaml(p) == "ti"


def test_repos_meta_arago_classifies_as_ti(tmp_path: Path) -> None:
    body = "repos:\n  meta-arago:\n    path: sources/meta-arago\n"
    p = _write(tmp_path, body)
    assert detect_bsp_from_yaml(p) == "ti"


def test_machine_takes_precedence_over_repos(tmp_path: Path) -> None:
    """Machine prefix wins even when the repos block points the other way."""
    body = "machine: imx95-var-dart\nrepos:\n  meta-ti-bsp:\n    path: sources/meta-ti-bsp\n"
    p = _write(tmp_path, body)
    assert detect_bsp_from_yaml(p) == "nxp"


def test_machine_qemuarm64_classifies_as_generic(tmp_path: Path) -> None:
    """A non-Variscite machine string falls through to the generic bucket."""
    p = _write(tmp_path, "machine: qemuarm64\n")
    assert detect_bsp_from_yaml(p) == "generic"


def test_poky_meta_arm_classifies_as_generic(tmp_path: Path) -> None:
    """A poky + meta-arm kas YAML with no Variscite markers is generic."""
    body = "machine: qemuarm64\nrepos:\n  poky:\n    path: sources/poky\n  meta-arm:\n    path: sources/meta-arm\n"
    p = _write(tmp_path, body)
    assert detect_bsp_from_yaml(p) == "generic"


def test_repos_only_with_generic_layer_classifies_as_generic(tmp_path: Path) -> None:
    body = "repos:\n  poky:\n    path: sources/poky\n"
    p = _write(tmp_path, body)
    assert detect_bsp_from_yaml(p) == "generic"


def test_empty_yaml_returns_unknown(tmp_path: Path) -> None:
    """An empty YAML has neither machine nor repos - reject."""
    p = _write(tmp_path, "")
    assert detect_bsp_from_yaml(p) == "unknown"


def test_yaml_without_machine_or_repos_returns_unknown(tmp_path: Path) -> None:
    """A YAML carrying only header/distro is too sparse to be a build."""
    p = _write(tmp_path, "header:\n  version: 3\ndistro: poky\n")
    assert detect_bsp_from_yaml(p) == "unknown"


def test_garbage_yaml_returns_unknown(tmp_path: Path) -> None:
    p = _write(tmp_path, "this is: not: valid:\n: yaml: at all:\n")
    assert detect_bsp_from_yaml(p) == "unknown"


def test_missing_file_returns_unknown(tmp_path: Path) -> None:
    assert detect_bsp_from_yaml(tmp_path / "does-not-exist.yml") == "unknown"


def test_real_nxp_example_classifies_as_nxp() -> None:
    """Smoke-test the shipped example."""
    from pathlib import Path as _P

    repo_root = _P(__file__).resolve().parent.parent
    example = repo_root / "examples" / "kas-imx95-var-dart.yml"
    assert example.is_file(), f"missing fixture: {example}"
    assert detect_bsp_from_yaml(example) == "nxp"


def test_real_ti_example_classifies_as_ti() -> None:
    """Smoke-test the shipped example."""
    from pathlib import Path as _P

    repo_root = _P(__file__).resolve().parent.parent
    example = repo_root / "examples" / "kas-am62x-var-som.yml"
    assert example.is_file(), f"missing fixture: {example}"
    assert detect_bsp_from_yaml(example) == "ti"


# ---------------------------------------------------------------------------
# is_meta_avocado_yaml
# ---------------------------------------------------------------------------


def test_meta_avocado_yaml_detected_when_in_path(tmp_path: Path) -> None:
    repo = tmp_path / "sources" / "meta-avocado" / "kas" / "machine"
    repo.mkdir(parents=True)
    p = repo / "qemux86-64.yml"
    p.write_text("machine: avocado-qemux86-64\n")
    assert is_meta_avocado_yaml(p) is True


def test_meta_avocado_yaml_not_detected_for_generic_yaml(tmp_path: Path) -> None:
    p = tmp_path / "build" / "kas.yml"
    p.parent.mkdir(parents=True)
    p.write_text("machine: qemuarm64\n")
    assert is_meta_avocado_yaml(p) is False


def test_meta_avocado_yaml_not_detected_for_nxp_yaml(tmp_path: Path) -> None:
    repo = tmp_path / "nxp" / "sources" / "meta-imx"
    repo.mkdir(parents=True)
    p = tmp_path / "nxp" / "kas-nxp.yml"
    p.write_text("machine: imx95-var-dart\n")
    assert is_meta_avocado_yaml(p) is False


# ---------------------------------------------------------------------------
# detect_kas_workspace
# ---------------------------------------------------------------------------


def test_detect_kas_workspace_returns_meta_avocado_parent(tmp_path: Path) -> None:
    """For a YAML inside meta-avocado, the workspace is the meta-avocado parent."""
    sources = tmp_path / "sources"
    repo = sources / "meta-avocado" / "kas" / "machine"
    repo.mkdir(parents=True)
    p = repo / "qemux86-64.yml"
    p.write_text("machine: avocado-qemux86-64\n")
    assert detect_kas_workspace(p) == sources


def test_detect_kas_workspace_returns_yaml_parent_for_plain_generic(tmp_path: Path) -> None:
    """For a non-meta-avocado YAML, the workspace is the YAML's parent."""
    build = tmp_path / "mybuild"
    build.mkdir()
    p = build / "kas.yml"
    p.write_text("machine: qemuarm64\n")
    assert detect_kas_workspace(p) == build
