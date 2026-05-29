"""Topology-only kas YAML generator for NXP i.MX and TI Sitara BSP workspaces.

Turns a repo-tool manifest XML (NXP) or an oe-layertool-populated tree
plus a bitbake-generated ``bblayers.conf`` into a deterministic kas
YAML covering machine, distro, target, and repos. The BSP tuning
block (ccache, MIRRORS, PREMIRRORS, FETCHCMD_wget, fork PREMIRRORs,
renderdoc fix, meta-varis-overrides repo) lives in the static overlays
under ``overlays/bakar-tuning-<bsp>.yml`` and is layered in by ``bakar
build`` at run time. Keeping topology and tuning in separate files
means BYO and manifest flows reuse the *same* tuning, with no risk of
drift between the two outputs.

All the prior bug fixes still apply:

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

import json
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
    ``overlays/bakar-tuning-<bsp>.yml`` and is layered on top by
    ``bakar build``. The topology generator only needs to know which
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
    """Parse a BSP-generated bblayers.conf into ``{repo: {layer, ...}}``.

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
        "header": {"version": 21},
        "distro": opts.distro,
        "machine": opts.machine,
        "target": opts.target,
        "repos": dict(repos),
    }


GEN_HEADER_COMMENT = """\
# This file is auto-generated by `bakar gen-kas` (bakar module).
# Source manifest: {manifest}
# Topology only - tuning is layered in from overlays/bakar-tuning-<bsp>.yml
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


# ---------------------------------------------------------------------------
# bitbake-setup workspace translation
# ---------------------------------------------------------------------------

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _extract_fragment_value(fragments: list[str], prefix: str) -> str | None:
    """Return the value of the first ``<prefix>/<value>`` fragment, or None.

    Entries with an empty suffix (e.g. a bare ``machine/``) are skipped.
    """
    for fragment in fragments:
        head, sep, value = fragment.partition("/")
        if head == prefix and sep and value:
            return value
    return None


def _load_bbsetup_config(
    setup_dir: Path,
) -> tuple[dict, dict[str, str], dict, set[str], dict[str, dict[str, None]]]:
    """Load and validate config-upstream.json + sources-fixed-revisions.json.

    Returns (sources, shas, bb_config, known_sources, layers_by_source).
    Raises ValueError on malformed/missing JSON or invalid structure.
    """
    config_path = setup_dir / "config" / "config-upstream.json"
    try:
        cfg = json.loads(config_path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"{config_path} is not valid JSON: {exc}") from exc
    data = cfg.get("data")
    if not isinstance(data, dict):
        raise ValueError(f"{config_path} is missing a top-level 'data' object; not a valid bitbake-setup config")
    sources = data.get("sources")
    if not isinstance(sources, dict) or not sources:
        raise ValueError(f"{config_path} has an empty or absent 'data.sources' block")

    sfr_path = setup_dir / "config" / "sources-fixed-revisions.json"
    shas: dict[str, str] = {}
    if sfr_path.exists():
        try:
            shas = json.loads(sfr_path.read_text()).get("sources", {})
        except json.JSONDecodeError as exc:
            raise ValueError(f"{sfr_path} is not valid JSON: {exc}") from exc

    known_sources = set(sources)
    bb_config = cfg.get("bitbake-config", {})

    # Map each source to the layer subdirs it contributes (bb-layers entries).
    layers_by_source: dict[str, dict[str, None]] = {name: {} for name in sources}
    for entry in bb_config.get("bb-layers", []):
        source, _, subdir = entry.partition("/")
        if source not in known_sources:
            raise ValueError(f"bb-layers entry {entry!r} references unknown source {source!r}")
        layers_by_source[source][subdir or "."] = None

    return sources, shas, bb_config, known_sources, layers_by_source


def _build_repo_entry(
    name: str,
    source: dict,
    shas: dict[str, str],
    layers_by_source: dict[str, dict[str, None]],
) -> dict:
    """Build a single kas v3 repo dict entry for one bitbake-setup source."""
    git_remote = source.get("git-remote")
    if git_remote is None:
        raise ValueError(
            f"source {name!r} has no 'git-remote' (local-path sources are out "
            "of scope); please file a bug if you need this"
        )
    uri = git_remote.get("uri")
    if not uri:
        raise ValueError(f"source {name!r} git-remote has no 'uri'")
    entry: dict[str, Any] = {
        "url": uri,
        "path": f"layers/{name}",
    }
    branch = git_remote.get("branch")
    if name in shas:
        # Pinned SHA: commit-only, no branch. kas validates commit
        # reachability from the branch tip after fetching; if the branch
        # has moved forward the check fails even though the commit exists.
        entry["commit"] = shas[name]
    else:
        rev = git_remote.get("rev")
        if rev and _SHA_RE.match(rev):
            # Same reasoning: a SHA rev without a branch avoids the
            # reachability check.
            entry["commit"] = rev
        elif rev:
            entry["branch"] = rev
        elif branch:
            entry["branch"] = branch
    entry["layers"] = layers_by_source[name]
    return entry


def translate_bbsetup_config(
    setup_dir: Path,
    *,
    target: str = "core-image-minimal",
    machine_override: str | None = None,
    distro_override: str | None = None,
) -> dict[str, Any]:
    """Translate a ``bitbake-setup`` workspace config into a kas YAML dict.

    Reads ``<setup_dir>/config/config-upstream.json`` (the resolved registry
    config bitbake-setup writes) and the optional
    ``<setup_dir>/config/sources-fixed-revisions.json`` (pinned SHAs). Produces
    a kas v3 topology dict (header + machine/distro/target + repos) that the
    existing kas pipeline can build, reusing the layers bitbake-setup already
    fetched under ``<setup_dir>/layers/``.

    Raises ``ValueError`` if either JSON file is malformed, if the config lacks a
    top-level ``data`` object or a non-empty ``data.sources`` block, if a source
    has no ``git-remote`` (local-path sources are out of scope), or if a
    ``bb-layers`` entry references an unknown source.
    """
    sources, shas, bb_config, known_sources, layers_by_source = _load_bbsetup_config(setup_dir)
    repos: OrderedDict[str, dict[str, Any]] = OrderedDict()
    for name, source in sources.items():
        repos[name] = _build_repo_entry(name, source, shas, layers_by_source)

    all_fragments = list(bb_config.get("oe-fragment-choices", {}).values()) + bb_config.get("oe-fragments", [])
    machine = machine_override or _extract_fragment_value(all_fragments, "machine")
    distro = distro_override or _extract_fragment_value(all_fragments, "distro") or "nodistro"

    return {
        "header": {"version": 21},
        "distro": distro,
        "machine": machine,
        "target": target,
        "repos": dict(repos),
    }


def write_bbsetup_yaml(
    setup_dir: Path,
    *,
    target: str = "core-image-minimal",
    machine_override: str | None = None,
    distro_override: str | None = None,
) -> Path:
    """Translate a bitbake-setup workspace and write ``kas-bbsetup.yml``.

    The output is written atomically to ``<setup_dir>/kas-bbsetup.yml`` and the
    path is returned. ``kas-bbsetup.yml`` is a generated build artifact: it is
    regenerated on every build and must not be committed to version control.
    """
    data = translate_bbsetup_config(
        setup_dir,
        target=target,
        machine_override=machine_override,
        distro_override=distro_override,
    )
    body = yaml.dump(
        data,
        Dumper=_Dumper,
        default_flow_style=False,
        sort_keys=True,
        width=120,
    )
    output = setup_dir / "kas-bbsetup.yml"
    tmp = output.with_suffix(output.suffix + ".tmp")
    tmp.write_text(body)
    tmp.replace(output)
    return output
