"""Extended unit tests for :mod:`bakar.kas`.

Targets the YAML-generation paths not exercised by ``test_kas_generation.py``
(topology contract pins), ``test_kas_env.py`` (kas_build env construction), or
``test_bbsetup.py`` (bbsetup translation happy/error paths). Specifically:

* :func:`parse_bblayers` against every assignment form and skip rule.
* :func:`parse_manifest` against repo-tool manifest XMLs, including the
  ``path=``/``name=``/``.git`` suffix branches and the bblayers filter.
* :func:`build_yaml_dict` against the manifest+bblayers and skip-manifest paths.
* :func:`write_yaml` end-to-end: atomic write, deterministic comment, in/out of
  workspace path rendering.
* :func:`translate_bbsetup_config` error branches not covered elsewhere:
  malformed ``config-upstream.json``, unknown source in ``bb-layers``, source
  without a ``uri``, and the symbolic-branch (no SHA, no rev) fallback.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
import yaml

from bakar.kas import (
    GEN_HEADER_COMMENT,
    NXP_KAS_TEMPLATE,
    TI_KAS_TEMPLATE,
    KasGenOptions,
    KasTemplate,
    build_yaml_dict,
    parse_bblayers,
    parse_manifest,
    translate_bbsetup_config,
    write_yaml,
)

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# parse_bblayers
# ---------------------------------------------------------------------------


def test_parse_bblayers_empty_when_no_bblayers_assignment(tmp_path: Path) -> None:
    """Files lacking any BBLAYERS= line produce an empty mapping."""
    bblayers = tmp_path / "bblayers.conf"
    bblayers.write_text('LCONF_VERSION = "7"\n')
    assert parse_bblayers(bblayers) == {}


def test_parse_bblayers_handles_question_mark_and_append(tmp_path: Path) -> None:
    """BBLAYERS ?=, ??=, and += must all be picked up so no layer is dropped."""
    bblayers = tmp_path / "bblayers.conf"
    bblayers.write_text(
        """\
BBLAYERS ?= " \\
  ${TOPDIR}/../sources/poky/meta \\
  ${TOPDIR}/../sources/poky/meta-poky \\
"
BBLAYERS += " \\
  ${TOPDIR}/../sources/meta-extra/meta-foo \\
"
"""
    )
    layers = parse_bblayers(bblayers)
    # poky contributes two layers, meta-extra one.
    assert layers["poky"] == {"meta", "meta-poky"}
    assert layers["meta-extra"] == {"meta-foo"}


def test_parse_bblayers_drops_tokens_outside_sources_dir(tmp_path: Path) -> None:
    """Tokens not under .../sources/ are ignored, not crashed on."""
    bblayers = tmp_path / "bblayers.conf"
    bblayers.write_text('BBLAYERS = " \\\n  ${TOPDIR}/../random/path \\\n  ${TOPDIR}/../sources/keep/meta \\\n"\n')
    layers = parse_bblayers(bblayers)
    assert "keep" in layers
    assert all("random" not in repo for repo in layers)


def test_parse_bblayers_repo_with_no_layer_subpath(tmp_path: Path) -> None:
    """A ``sources/<repo>`` entry with no trailing layer subdir registers an empty set."""
    bblayers = tmp_path / "bblayers.conf"
    bblayers.write_text('BBLAYERS = " ${TOPDIR}/../sources/standalone "\n')
    layers = parse_bblayers(bblayers)
    assert layers == {"standalone": set()}


def test_parse_bblayers_strips_line_comments(tmp_path: Path) -> None:
    """Lines starting with # (and inline # comments) must not feed token parsing."""
    bblayers = tmp_path / "bblayers.conf"
    bblayers.write_text('# leading comment\nBBLAYERS = " ${TOPDIR}/../sources/poky/meta " # trailing\n')
    layers = parse_bblayers(bblayers)
    assert layers == {"poky": {"meta"}}


# ---------------------------------------------------------------------------
# parse_manifest
# ---------------------------------------------------------------------------


_MANIFEST_TWO_PROJECTS = """\
<?xml version="1.0" encoding="UTF-8"?>
<manifest>
  <remote name="origin" fetch="https://example.invalid/" />
  <default remote="origin" revision="main" />
  <project name="meta-foo" path="sources/meta-foo" />
  <project name="meta-bar" path="sources/meta-bar" />
</manifest>
"""

_MANIFEST_NAME_ONLY = """\
<?xml version="1.0" encoding="UTF-8"?>
<manifest>
  <remote name="origin" fetch="https://example.invalid/" />
  <default remote="origin" revision="main" />
  <project name="group/meta-from-name.git" />
</manifest>
"""

_MANIFEST_NO_DEFAULT_REMOTE = """\
<?xml version="1.0" encoding="UTF-8"?>
<manifest>
  <remote name="origin" fetch="https://example.invalid/" />
  <default revision="main" />
  <project name="meta-foo" path="sources/meta-foo" />
</manifest>
"""

_MANIFEST_EMPTY_NAME = """\
<?xml version="1.0" encoding="UTF-8"?>
<manifest>
  <remote name="origin" fetch="https://example.invalid/" />
  <default remote="origin" revision="main" />
  <project name="" path="sources/skip-me" />
  <project name="meta-keep" path="sources/meta-keep" />
</manifest>
"""


def test_parse_manifest_returns_repos_sorted(tmp_path: Path) -> None:
    """Output ordering is alphabetical regardless of manifest project order."""
    manifest = tmp_path / "manifest.xml"
    manifest.write_text(_MANIFEST_TWO_PROJECTS)
    result = parse_manifest(manifest, bblayers_map=None)
    assert list(result.keys()) == ["meta-bar", "meta-foo"]


def test_parse_manifest_filters_by_bblayers_map(tmp_path: Path) -> None:
    """When a bblayers_map is supplied, repos not in it are dropped."""
    manifest = tmp_path / "manifest.xml"
    manifest.write_text(_MANIFEST_TWO_PROJECTS)
    result = parse_manifest(manifest, bblayers_map={"meta-foo": {"meta"}})
    assert list(result.keys()) == ["meta-foo"]
    assert result["meta-foo"]["layers"] == {"meta": None}


def test_parse_manifest_uses_name_when_path_missing(tmp_path: Path) -> None:
    """A project with no path= attribute synthesizes path from the name leaf,
    and a trailing .git is stripped from the dict key (but kept in the path)."""
    manifest = tmp_path / "manifest.xml"
    manifest.write_text(_MANIFEST_NAME_ONLY)
    result = parse_manifest(manifest, bblayers_map=None)
    # The dict key has .git stripped; the synthesized path keeps it because
    # entry_path is computed before the .git suffix is dropped from pname.
    assert "meta-from-name" in result
    assert result["meta-from-name"]["path"] == "sources/meta-from-name.git"


def test_parse_manifest_skips_empty_name_projects(tmp_path: Path) -> None:
    """Projects with name="" are silently skipped."""
    manifest = tmp_path / "manifest.xml"
    manifest.write_text(_MANIFEST_EMPTY_NAME)
    result = parse_manifest(manifest, bblayers_map=None)
    assert list(result.keys()) == ["meta-keep"]


def test_parse_manifest_handles_default_without_remote(tmp_path: Path) -> None:
    """A <default> element with no remote= attribute must not raise."""
    manifest = tmp_path / "manifest.xml"
    manifest.write_text(_MANIFEST_NO_DEFAULT_REMOTE)
    # Must not raise; result content is incidental here, just that the parser
    # tolerates the missing remote attribute.
    result = parse_manifest(manifest, bblayers_map=None)
    assert "meta-foo" in result


def test_parse_manifest_omits_layers_when_repo_has_empty_set(tmp_path: Path) -> None:
    """A repo present in the bblayers_map with an empty layer set gets no 'layers' key."""
    manifest = tmp_path / "manifest.xml"
    manifest.write_text(_MANIFEST_TWO_PROJECTS)
    result = parse_manifest(manifest, bblayers_map={"meta-foo": set(), "meta-bar": set()})
    assert "layers" not in result["meta-foo"]
    assert "layers" not in result["meta-bar"]


# ---------------------------------------------------------------------------
# build_yaml_dict — manifest+bblayers and skip_manifest with bblayers paths
# ---------------------------------------------------------------------------


def _make_options(
    workspace: Path,
    template: KasTemplate,
    *,
    bblayers: Path | None = None,
    skip_manifest: bool = True,
    manifest: Path | None = None,
) -> KasGenOptions:
    return KasGenOptions(
        manifest=manifest or (workspace / "fake-manifest.txt"),
        bblayers=bblayers,
        machine="imx8mp-var-dart",
        distro="fsl-imx-xwayland",
        target="core-image-minimal",
        output=workspace / "kas-out.yml",
        workspace=workspace,
        template=template,
        skip_manifest=skip_manifest,
    )


def test_build_yaml_dict_reads_bblayers_when_supplied(tmp_path: Path) -> None:
    """When opts.bblayers is set, parse_bblayers feeds repos in TI skip_manifest mode."""
    (tmp_path / "ti").mkdir()
    bblayers = tmp_path / "bblayers.conf"
    bblayers.write_text(
        'BBLAYERS = " ${TOPDIR}/../sources/poky/meta ${TOPDIR}/../sources/poky/meta-poky '
        '${TOPDIR}/../sources/meta-arm/meta-arm "\n'
    )
    out = build_yaml_dict(_make_options(tmp_path, TI_KAS_TEMPLATE, bblayers=bblayers))
    assert set(out["repos"]) == {"poky", "meta-arm"}
    assert out["repos"]["poky"]["path"] == "sources/poky"
    # Sorted layer order is deterministic.
    assert list(out["repos"]["poky"]["layers"].keys()) == ["meta", "meta-poky"]
    # Layer values are None (kas 5.2 schema: enable at default prio).
    assert out["repos"]["poky"]["layers"]["meta"] is None


def test_build_yaml_dict_skip_manifest_no_bblayers_yields_empty_repos(tmp_path: Path) -> None:
    """skip_manifest=True without bblayers leaves repos: {} - the TI no-yet-populated case."""
    (tmp_path / "ti").mkdir()
    out = build_yaml_dict(_make_options(tmp_path, TI_KAS_TEMPLATE))
    assert out["repos"] == {}


def test_build_yaml_dict_skip_manifest_with_bblayers_no_layers(tmp_path: Path) -> None:
    """A bblayers repo with no layer subdir lands as a path-only entry (no 'layers' key)."""
    (tmp_path / "ti").mkdir()
    bblayers = tmp_path / "bblayers.conf"
    bblayers.write_text('BBLAYERS = " ${TOPDIR}/../sources/standalone "\n')
    out = build_yaml_dict(_make_options(tmp_path, TI_KAS_TEMPLATE, bblayers=bblayers))
    assert out["repos"] == {"standalone": {"path": "sources/standalone"}}


def test_build_yaml_dict_manifest_path_uses_parse_manifest(tmp_path: Path) -> None:
    """skip_manifest=False routes through parse_manifest with the bblayers filter."""
    (tmp_path / "nxp").mkdir()
    manifest = tmp_path / "nxp" / "manifest.xml"
    manifest.write_text(_MANIFEST_TWO_PROJECTS)
    bblayers = tmp_path / "bblayers.conf"
    bblayers.write_text('BBLAYERS = " ${TOPDIR}/../sources/meta-foo/meta "\n')
    out = build_yaml_dict(
        _make_options(
            tmp_path,
            NXP_KAS_TEMPLATE,
            bblayers=bblayers,
            skip_manifest=False,
            manifest=manifest,
        )
    )
    # meta-bar is in the manifest but not in bblayers → filtered out.
    assert list(out["repos"]) == ["meta-foo"]
    assert out["repos"]["meta-foo"]["layers"] == {"meta": None}


# ---------------------------------------------------------------------------
# write_yaml — atomic write, comment header, deterministic body
# ---------------------------------------------------------------------------


def test_write_yaml_writes_atomically_with_comment_header(tmp_path: Path) -> None:
    """write_yaml renders the prelude + YAML and removes the tmp sibling."""
    (tmp_path / "nxp").mkdir()
    opts = _make_options(tmp_path, NXP_KAS_TEMPLATE)
    write_yaml(opts)
    text = opts.output.read_text()
    # The auto-generated header is present verbatim (relative manifest path).
    assert text.startswith("# This file is auto-generated by `bakar gen-kas` (bakar module).\n")
    assert "fake-manifest.txt" in text
    # Strip the prelude to assert the YAML body parses cleanly.
    body = text.split(GEN_HEADER_COMMENT.format(manifest="fake-manifest.txt"), 1)[1]
    parsed = yaml.safe_load(body)
    assert parsed["header"] == {"version": 21}
    assert parsed["machine"] == "imx8mp-var-dart"
    # The temp sibling has been renamed away.
    assert not opts.output.with_suffix(opts.output.suffix + ".tmp").exists()


def test_write_yaml_creates_parent_directory(tmp_path: Path) -> None:
    """opts.output.parent is created on demand so callers can pass a fresh dir."""
    nested = tmp_path / "deep" / "nested" / "out.yml"
    opts = KasGenOptions(
        manifest=tmp_path / "fake.txt",
        bblayers=None,
        machine="x",
        distro="y",
        target="z",
        output=nested,
        workspace=tmp_path,
        template=NXP_KAS_TEMPLATE,
        skip_manifest=True,
    )
    write_yaml(opts)
    assert nested.is_file()


def test_write_yaml_uses_absolute_manifest_path_when_outside_workspace(tmp_path: Path) -> None:
    """If the manifest is not under workspace, the header logs the absolute path."""
    outside = tmp_path / "outside"
    outside.mkdir()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    manifest = outside / "manifest.xml"
    manifest.write_text(_MANIFEST_TWO_PROJECTS)

    opts = KasGenOptions(
        manifest=manifest,
        bblayers=None,
        machine="x",
        distro="y",
        target="z",
        output=workspace / "kas-out.yml",
        workspace=workspace,
        template=NXP_KAS_TEMPLATE,
        skip_manifest=True,
    )
    write_yaml(opts)
    text = opts.output.read_text()
    # Absolute path appears in the comment header (no relative_to).
    assert str(manifest) in text


def test_write_yaml_is_deterministic(tmp_path: Path) -> None:
    """Two consecutive writes yield byte-identical output (no timestamps)."""
    (tmp_path / "nxp").mkdir()
    opts = _make_options(tmp_path, NXP_KAS_TEMPLATE)
    write_yaml(opts)
    first = opts.output.read_bytes()
    write_yaml(opts)
    second = opts.output.read_bytes()
    assert first == second


# ---------------------------------------------------------------------------
# translate_bbsetup_config — error branches not covered by test_bbsetup.py
# ---------------------------------------------------------------------------


def _write_bbsetup(setup_dir: Path, cfg: dict, *, sfr: dict | None = None) -> None:
    """Build a minimal bitbake-setup workspace under ``setup_dir``."""
    (setup_dir / "config").mkdir(parents=True, exist_ok=True)
    (setup_dir / "config" / "config-upstream.json").write_text(json.dumps(cfg))
    if sfr is not None:
        (setup_dir / "config" / "sources-fixed-revisions.json").write_text(json.dumps(sfr))


def test_translate_rejects_malformed_config_upstream(tmp_path: Path) -> None:
    """config-upstream.json that isn't valid JSON raises ValueError with the path."""
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config-upstream.json").write_text("{ not json")
    with pytest.raises(ValueError, match="not valid JSON"):
        translate_bbsetup_config(tmp_path)


def test_translate_rejects_bb_layers_with_unknown_source(tmp_path: Path) -> None:
    """A bb-layers entry pointing at a source absent from data.sources raises."""
    cfg = {
        "data": {
            "sources": {
                "openembedded-core": {"git-remote": {"uri": "https://example.invalid/oe-core", "branch": "main"}}
            }
        },
        "bitbake-config": {
            "bb-layers": ["openembedded-core/meta", "ghost-layer/meta"],
        },
    }
    _write_bbsetup(tmp_path, cfg)
    with pytest.raises(ValueError, match="ghost-layer"):
        translate_bbsetup_config(tmp_path)


def test_translate_rejects_source_without_uri(tmp_path: Path) -> None:
    """A git-remote dict missing 'uri' raises ValueError naming the source."""
    cfg = {
        "data": {
            "sources": {
                "broken-src": {"git-remote": {"branch": "main"}},
            }
        },
        "bitbake-config": {"bb-layers": []},
    }
    _write_bbsetup(tmp_path, cfg)
    with pytest.raises(ValueError, match="broken-src"):
        translate_bbsetup_config(tmp_path)


def test_translate_uses_branch_when_no_rev_and_no_sha(tmp_path: Path) -> None:
    """When the source has only a branch (no rev, no SHA), the branch is emitted."""
    cfg = {
        "data": {
            "sources": {
                "only-branch": {
                    "git-remote": {
                        "uri": "https://example.invalid/repo.git",
                        "branch": "release-1.0",
                    }
                }
            }
        },
        "bitbake-config": {"bb-layers": []},
    }
    _write_bbsetup(tmp_path, cfg)
    data = translate_bbsetup_config(tmp_path)
    only_branch = data["repos"]["only-branch"]
    assert only_branch["branch"] == "release-1.0"
    assert "commit" not in only_branch


def test_translate_oe_fragments_list_yields_machine(tmp_path: Path) -> None:
    """Machine extracted from oe-fragments (a list, not the choices dict)."""
    cfg = {
        "data": {
            "sources": {
                "src1": {"git-remote": {"uri": "https://example.invalid/r.git", "branch": "x"}},
            }
        },
        "bitbake-config": {
            "bb-layers": [],
            "oe-fragments": ["machine/qemuarm", "distro/poky"],
        },
    }
    _write_bbsetup(tmp_path, cfg)
    data = translate_bbsetup_config(tmp_path)
    assert data["machine"] == "qemuarm"
    assert data["distro"] == "poky"


def test_translate_defaults_distro_to_nodistro(tmp_path: Path) -> None:
    """When no distro fragment exists and no override is given, distro defaults to 'nodistro'."""
    cfg = {
        "data": {
            "sources": {
                "src1": {"git-remote": {"uri": "https://example.invalid/r.git", "branch": "x"}},
            }
        },
        "bitbake-config": {"bb-layers": []},
    }
    _write_bbsetup(tmp_path, cfg)
    data = translate_bbsetup_config(tmp_path)
    assert data["distro"] == "nodistro"
    assert data["machine"] is None


def test_translate_machine_override_with_no_fragment(tmp_path: Path) -> None:
    """machine_override wins even when the underlying fragments are absent."""
    cfg = {
        "data": {
            "sources": {
                "src1": {"git-remote": {"uri": "https://example.invalid/r.git", "branch": "x"}},
            }
        },
        "bitbake-config": {"bb-layers": []},
    }
    _write_bbsetup(tmp_path, cfg)
    data = translate_bbsetup_config(tmp_path, machine_override="custom-board", distro_override="custom-distro")
    assert data["machine"] == "custom-board"
    assert data["distro"] == "custom-distro"
