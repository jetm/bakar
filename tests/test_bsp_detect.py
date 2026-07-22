"""Unit tests for bakar.bsp_detect.detect_bsp_from_yaml.

Pins the rules that classify a kas YAML as NXP, TI, generic, or
unknown for the BYO ``bakar build my.yml`` flow. Order: machine
prefix wins, then repos block names, then a generic fallback for
parseable YAMLs that have at least one of those anchors.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bakar.bsp_detect import (
    detect_bsp_from_yaml,
    detect_kas_workspace,
    is_bbsetup_workspace,
    is_meta_avocado_yaml,
    machine_from_yaml,
)

pytestmark = pytest.mark.unit


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
    """A non-NXP/TI machine string falls through to the generic bucket."""
    p = _write(tmp_path, "machine: qemuarm64\n")
    assert detect_bsp_from_yaml(p) == "generic"


def test_poky_meta_arm_classifies_as_generic(tmp_path: Path) -> None:
    """A poky + meta-arm kas YAML with no NXP/TI markers is generic."""
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
    p = _write(tmp_path, "header:\n  version: 21\ndistro: poky\n")
    assert detect_bsp_from_yaml(p) == "unknown"


def test_garbage_yaml_returns_unknown(tmp_path: Path) -> None:
    p = _write(tmp_path, "this is: not: valid:\n: yaml: at all:\n")
    assert detect_bsp_from_yaml(p) == "unknown"


def test_include_only_yaml_classifies_as_generic(tmp_path: Path) -> None:
    """An include-only wrapper (header.includes + local_conf_header) is a
    valid kas config - the standard way to layer a tweak on a base YAML."""
    (tmp_path / "base.yml").write_text("header:\n  version: 21\nmachine: qemux86-64\n", encoding="utf-8")
    body = (
        "header:\n  version: 21\n  includes:\n    - base.yml\n"
        'local_conf_header:\n  inject: |\n    PNBLACKLIST[m4-native] = "test"\n'
    )
    p = _write(tmp_path, body)
    assert detect_bsp_from_yaml(p) == "generic"


def test_include_wrapper_inherits_nxp_family(tmp_path: Path) -> None:
    """A wrapper over an NXP YAML must classify as nxp so the family overlay
    (ACCEPT_FSL_EULA etc.) is layered, not the generic one."""
    (tmp_path / "base.yml").write_text("header:\n  version: 21\nmachine: imx8mp-var-dart\n", encoding="utf-8")
    body = "header:\n  version: 21\n  includes:\n    - base.yml\n"
    p = _write(tmp_path, body)
    assert detect_bsp_from_yaml(p) == "nxp"


def test_include_wrapper_with_missing_base_classifies_as_generic(tmp_path: Path) -> None:
    """A dangling include still classifies generic; kas reports the missing
    file with its own clearer error at build time."""
    body = "header:\n  version: 21\n  includes:\n    - does-not-exist.yml\n"
    p = _write(tmp_path, body)
    assert detect_bsp_from_yaml(p) == "generic"


def test_include_cycle_terminates_as_generic(tmp_path: Path) -> None:
    """Mutually-including YAMLs must terminate via the depth cap."""
    (tmp_path / "a.yml").write_text("header:\n  version: 21\n  includes:\n    - b.yml\n", encoding="utf-8")
    (tmp_path / "b.yml").write_text("header:\n  version: 21\n  includes:\n    - a.yml\n", encoding="utf-8")
    assert detect_bsp_from_yaml(tmp_path / "a.yml") == "generic"


def test_empty_includes_list_returns_unknown(tmp_path: Path) -> None:
    """header.includes: [] carries no build anchors - still rejected."""
    p = _write(tmp_path, "header:\n  version: 21\n  includes: []\n")
    assert detect_bsp_from_yaml(p) == "unknown"


def test_missing_file_returns_unknown(tmp_path: Path) -> None:
    assert detect_bsp_from_yaml(tmp_path / "does-not-exist.yml") == "unknown"


def test_real_nxp_example_classifies_as_nxp() -> None:
    """Smoke-test the shipped example."""
    repo_root = Path(__file__).resolve().parent.parent
    example = repo_root / "examples" / "kas-imx95-var-dart.yml"
    assert example.is_file(), f"missing fixture: {example}"
    assert detect_bsp_from_yaml(example) == "nxp"


def test_real_ti_example_classifies_as_ti() -> None:
    """Smoke-test the shipped example."""
    repo_root = Path(__file__).resolve().parent.parent
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


def test_detect_kas_workspace_generated_yaml_finds_bakar_toml_root(tmp_path: Path) -> None:
    """A generated build YAML outside meta-avocado resolves to the .bakar.toml root."""
    root = tmp_path / "sources"
    build = root / "build-qemux86-64"
    build.mkdir(parents=True)
    (root / ".bakar.toml").write_text("[build]\n")
    p = build / "avocado-bakar.yml"
    p.write_text("machine: avocado-qemux86-64\n")
    assert detect_kas_workspace(p) == root


def test_detect_kas_workspace_generated_yaml_finds_meta_avocado_sibling_root(tmp_path: Path) -> None:
    """Without a .bakar.toml, a meta-avocado/ sibling marks the workspace root."""
    root = tmp_path / "sources"
    (root / "meta-avocado").mkdir(parents=True)
    build = root / "build-x"
    build.mkdir()
    p = build / "foo.yml"
    p.write_text("machine: qemuarm64\n")
    assert detect_kas_workspace(p) == root


# ---------------------------------------------------------------------------
# is_bbsetup_workspace
# ---------------------------------------------------------------------------


_VALID_BBSETUP_CONFIG: dict = {
    "type": "registry",
    "name": "oe-nodistro-wrynose",
    "data": {
        "sources": {
            "openembedded-core": {
                "git-remote": {"uri": "https://git.openembedded.org/openembedded-core", "branch": "wrynose"}
            }
        }
    },
    "bitbake-config": {
        "name": "nodistro",
        "bb-layers": ["openembedded-core/meta"],
    },
}


def _make_bbsetup_workspace(root: Path, *, config: object | str, with_env: bool = True) -> Path:
    """Build a bitbake-setup workspace under ``root`` and return ``root``.

    ``config`` is dumped to ``config/config-upstream.json`` as JSON when a
    dict/list, or written verbatim when a raw string (for malformed-JSON
    cases). ``with_env`` toggles the ``build/init-build-env`` sentinel.
    """
    (root / "config").mkdir(parents=True)
    cfg_path = root / "config" / "config-upstream.json"
    if isinstance(config, str):
        cfg_path.write_text(config, encoding="utf-8")
    else:
        cfg_path.write_text(json.dumps(config), encoding="utf-8")
    if with_env:
        (root / "build").mkdir(parents=True)
        (root / "build" / "init-build-env").write_text("", encoding="utf-8")
    return root


def test_bbsetup_fully_initialized_workspace_returns_true(tmp_path: Path) -> None:
    ws = _make_bbsetup_workspace(tmp_path / "ws", config=_VALID_BBSETUP_CONFIG)
    assert is_bbsetup_workspace(ws) is True


def test_bbsetup_missing_init_build_env_returns_false(tmp_path: Path) -> None:
    ws = _make_bbsetup_workspace(tmp_path / "ws", config=_VALID_BBSETUP_CONFIG, with_env=False)
    assert is_bbsetup_workspace(ws) is False


def test_bbsetup_malformed_json_returns_false_without_raising(tmp_path: Path) -> None:
    ws = _make_bbsetup_workspace(tmp_path / "ws", config="{not valid json")
    assert is_bbsetup_workspace(ws) is False


def test_bbsetup_valid_json_missing_data_returns_false(tmp_path: Path) -> None:
    config = {"bitbake-config": _VALID_BBSETUP_CONFIG["bitbake-config"]}
    ws = _make_bbsetup_workspace(tmp_path / "ws", config=config)
    assert is_bbsetup_workspace(ws) is False


def test_bbsetup_valid_json_missing_bitbake_config_returns_false(tmp_path: Path) -> None:
    config = {"data": _VALID_BBSETUP_CONFIG["data"]}
    ws = _make_bbsetup_workspace(tmp_path / "ws", config=config)
    assert is_bbsetup_workspace(ws) is False


def test_machine_from_yaml_reads_top_level_key(tmp_path: Path) -> None:
    p = _write(tmp_path, "machine: avocado-qemuarm64\nrepos: {}\n")
    assert machine_from_yaml(p) == "avocado-qemuarm64"


def test_machine_from_yaml_strips_whitespace(tmp_path: Path) -> None:
    p = _write(tmp_path, 'machine: "  qemux86-64  "\n')
    assert machine_from_yaml(p) == "qemux86-64"


def test_machine_from_yaml_absent_returns_none(tmp_path: Path) -> None:
    p = _write(tmp_path, "repos:\n  poky:\n    path: sources/poky\n")
    assert machine_from_yaml(p) is None


def test_machine_from_yaml_follows_includes_last_wins(tmp_path: Path) -> None:
    (tmp_path / "base-a.yml").write_text("machine: first\n", encoding="utf-8")
    (tmp_path / "base-b.yml").write_text("machine: second\n", encoding="utf-8")
    entry = tmp_path / "entry.yml"
    entry.write_text("header:\n  includes:\n    - base-a.yml\n    - base-b.yml\n", encoding="utf-8")
    assert machine_from_yaml(entry) == "second"


def test_machine_from_yaml_top_level_wins_over_includes(tmp_path: Path) -> None:
    (tmp_path / "base.yml").write_text("machine: from-include\n", encoding="utf-8")
    entry = tmp_path / "entry.yml"
    entry.write_text("machine: from-entry\nheader:\n  includes:\n    - base.yml\n", encoding="utf-8")
    assert machine_from_yaml(entry) == "from-entry"


def test_machine_from_yaml_missing_file_returns_none(tmp_path: Path) -> None:
    assert machine_from_yaml(tmp_path / "nope.yml") is None


def test_machine_from_yaml_unparseable_returns_none(tmp_path: Path) -> None:
    p = _write(tmp_path, "machine: [unterminated\n")
    assert machine_from_yaml(p) is None
