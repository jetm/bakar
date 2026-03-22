"""Topology-only kas YAML generator for the Variscite BSP workspace.

Turns a repo-tool manifest XML (NXP) or an oe-layertool-populated tree
plus a bitbake-generated ``bblayers.conf`` into a deterministic kas
YAML covering machine, distro, target, and repos. The Variscite tuning
block (ccache, MIRRORS, PREMIRRORS, FETCHCMD_wget, fork PREMIRRORs,
renderdoc fix, meta-varis-overrides repo) lives in the static overlays
under ``overlays/varis-tuning-<bsp>.yml`` and is layered in by ``varis
build`` at run time. Keeping topology and tuning in separate files
means BYO and manifest flows reuse the *same* tuning, with no risk of
drift between the two outputs.

All the bug fixes from VARIS-03 still apply:

* ``<default>`` with no ``remote=`` attribute no longer crashes
* Repos with ``name`` != ``path-leaf`` (e.g. ``variscite-bsp-base`` ->
  ``sources/base``) emit the correct path and dict key
* ``BBLAYERS +=`` lines are captured alongside ``BBLAYERS =`` and
  ``BBLAYERS ?=`` so no layer is silently dropped
* kas 5.2 layer values are emitted as ``null`` (enable at default prio),
  not empty strings (which fail schema validation)
* Paths are rooted at ``KAS_WORK_DIR`` (no ``../`` prefix)
* Host-tooling repos that carry no layers are skipped
"""

from __future__ import annotations

import re
import xml.dom.minidom
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Template plumbing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KasTemplate:
    """Per-BSP knobs that drive ``build_yaml_dict``.

    Now slim - the optimization stack (ccache, MIRRORS, PREMIRRORS,
    fork PREMIRRORs, override layer) lives in the overlay YAMLs at
    ``overlays/varis-tuning-<bsp>.yml`` and is layered on top by
    ``varis build``. The topology generator only needs to know which
    BSP subdirectory the manifest belongs to so it can compute paths.
    """

    workspace_subdir: str  # "nxp" or "ti"


@dataclass(frozen=True)
class KasGenOptions:
    manifest: Path
    bblayers: Path | None
    machine: str
    distro: str
    target: str
    output: Path
    workspace: Path
    template: KasTemplate
    # When True, skip manifest XML parsing and emit an empty repos: {}
    # block. TI builds populate sources/ via oe-layertool-setup.sh; the
    # config file is a key=value text format, not XML.
    skip_manifest: bool = False


# ---------------------------------------------------------------------------
# bblayers.conf parser
# ---------------------------------------------------------------------------


def parse_bblayers(path: Path) -> dict[str, set[str]]:
    """Parse a Variscite-generated bblayers.conf into ``{repo: {layer, ...}}``.

    Tolerates ``=``, ``?=``, ``??=``, and ``+=`` forms; skips tokens that do
    not match ``.../sources/<repo>[/<layer>]``. Returns an empty dict when no
    BBLAYERS assignment is found.
    """
    text = path.read_text()
    # Strip `#` comments line-by-line, then join lines and collapse
    # backslash continuations.
    joined = " ".join(line.split("#", 1)[0] for line in text.splitlines()).replace("\\", " ")
    matches = re.findall(r'BBLAYERS\s*(?:\?\??|\+)?=\s*"([^"]*)"', joined)
    if not matches:
        return {}
    layers_map: dict[str, set[str]] = {}
    for body in matches:
        for token in body.split():
            token = token.strip()
            if not token:
                continue
            idx = token.find("/sources/")
            if idx == -1:
                continue
            rel = token[idx + len("/sources/") :].strip("/")
            if not rel:
                continue
            parts = rel.split("/")
            repo = parts[0]
            layers_map.setdefault(repo, set())
            if len(parts) >= 2 and parts[1]:
                layers_map[repo].add(parts[1])
    return layers_map


# ---------------------------------------------------------------------------
# manifest XML parser
# ---------------------------------------------------------------------------


def parse_manifest(manifest_path: Path, bblayers_map: dict[str, set[str]] | None) -> OrderedDict[str, dict[str, Any]]:
    """Return an ordered mapping of ``repo_name -> {path, layers?}``.

    When ``bblayers_map`` is supplied, only repos that appear in it are
    emitted - host-tooling repos like ``variscite-bsp-base`` (``sources/base``)
    or ``var-host-docker-containers`` are skipped entirely.
    """
    doc = xml.dom.minidom.parse(str(manifest_path))
    remotes = doc.getElementsByTagName("remote")
    defaults = doc.getElementsByTagName("default")
    projects = doc.getElementsByTagName("project")

    rems = {rem.getAttribute("name"): rem.getAttribute("fetch") for rem in remotes}
    def_remote_name = defaults[0].getAttribute("remote") if defaults else ""
    # Not actually emitted today (Pattern 2: kas uses existing checkouts),
    # but computed here in case a future caller needs it.
    _def_remote = rems[def_remote_name] if def_remote_name else ""
    _def_rev = defaults[0].getAttribute("revision") if defaults else ""

    data: OrderedDict[str, dict[str, Any]] = OrderedDict()
    for proj in projects:
        name = proj.getAttribute("name")
        if not name:
            continue
        path = proj.getAttribute("path")
        if path:
            pname = path.rsplit("/", 1)[-1]
            entry_path = path
        else:
            pname = name.rsplit("/", 1)[-1]
            entry_path = f"sources/{pname}"
        if pname.endswith(".git"):
            pname = pname[: -len(".git")]
        if bblayers_map is not None and pname not in bblayers_map:
            continue
        entry: dict[str, Any] = {"path": entry_path}
        if bblayers_map and bblayers_map.get(pname):
            entry["layers"] = {layer: None for layer in sorted(bblayers_map[pname])}
        data[pname] = entry
    # Sort so output is deterministic regardless of manifest ordering.
    return OrderedDict(sorted(data.items()))


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


NXP_KAS_TEMPLATE = KasTemplate(workspace_subdir="nxp")
TI_KAS_TEMPLATE = KasTemplate(workspace_subdir="ti")


# ---------------------------------------------------------------------------
# YAML emission
# ---------------------------------------------------------------------------


def _literal_representer(dumper: yaml.Dumper, data: str) -> yaml.ScalarNode:
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_EmitterYaml = yaml.SafeDumper


class _Dumper(_EmitterYaml):
    pass


_Dumper.add_representer(str, _literal_representer)


def build_yaml_dict(opts: KasGenOptions) -> dict[str, Any]:
    """Compose the topology-only kas YAML dict (header + machine/distro/target + repos).

    Tuning (local_conf_header) and the override layer (meta-varis-overrides)
    live in the static overlay YAMLs and are layered in at build time.
    """
    bblayers_map: dict[str, set[str]] | None = None
    if opts.bblayers is not None:
        bblayers_map = parse_bblayers(opts.bblayers)

    if opts.skip_manifest:
        # No manifest XML to parse (TI workflow): synthesize the repos
        # block directly from the bblayers map. ``path: sources/<repo>``
        # mirrors the on-disk layout that ``oe-layertool-setup.sh``
        # produces, and the in-container view via KAS_WORK_DIR.
        repos: OrderedDict[str, dict[str, Any]] = OrderedDict()
        if bblayers_map is not None:
            for repo_name in sorted(bblayers_map):
                entry: dict[str, Any] = {"path": f"sources/{repo_name}"}
                if bblayers_map[repo_name]:
                    entry["layers"] = {layer: None for layer in sorted(bblayers_map[repo_name])}
                repos[repo_name] = entry
    else:
        repos = parse_manifest(opts.manifest, bblayers_map)

    return {
        "header": {"version": 3},
        "distro": opts.distro,
        "machine": opts.machine,
        "target": opts.target,
        "repos": dict(repos),
    }


GEN_HEADER_COMMENT = """\
# This file is auto-generated by `varis gen-kas` (varis/varis_build).
# Source manifest: {manifest}
# Topology only - tuning is layered in from overlays/varis-tuning-<bsp>.yml
# at build time. Do not hand-edit. Re-run the converter to regenerate.
# A timestamp is intentionally omitted so the output is deterministic:
# two consecutive runs produce identical bytes for unchanged inputs.
"""


def write_yaml(opts: KasGenOptions) -> None:
    """Render the kas YAML and write it atomically to ``opts.output``."""
    data = build_yaml_dict(opts)
    comment = GEN_HEADER_COMMENT.format(
        manifest=opts.manifest.relative_to(opts.workspace)
        if opts.manifest.is_relative_to(opts.workspace)
        else opts.manifest,
    )
    body = yaml.dump(
        data,
        Dumper=_Dumper,
        default_flow_style=False,
        sort_keys=True,
        width=120,
    )
    opts.output.parent.mkdir(parents=True, exist_ok=True)
    tmp = opts.output.with_suffix(opts.output.suffix + ".tmp")
    tmp.write_text(comment + body)
    tmp.replace(opts.output)
