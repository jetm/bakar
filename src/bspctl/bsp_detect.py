"""Inspect a kas YAML and classify the BSP family.

Used by the BYO (Form A) path of ``varis build``: when the user hands
varis a kas YAML directly, we cannot rely on the manifest filename
regex in :func:`bspctl.bsp_model.detect_bsp_family`. Instead the
classifier reads ``machine:`` and ``repos:`` from the YAML and applies
a small first-match-wins rule set:

* machine starts with ``imx``                                  -> ``nxp``
* machine starts with ``am`` / ``k3-`` / ``j7-``               -> ``ti``
* repos contains ``meta-imx`` / ``meta-freescale*`` / ``meta-nxp*`` -> ``nxp``
* repos contains ``meta-ti-bsp`` / ``meta-ti`` / ``meta-arago`` -> ``ti``
* parseable YAML with at least ``machine:`` or ``repos:``      -> ``generic``
* unparseable / empty                                          -> ``unknown``

The ``generic`` classification is the BSP-agnostic fallback for kas
YAMLs that look like real builds but do not target a Variscite SoM
(e.g. qemuarm64 + poky + meta-arm). Callers layer the
``bspctl-tuning-generic.yml`` overlay - which carries only the
BSP-agnostic optimizations (ccache, MIRRORS, PREMIRRORS, FETCHCMD_wget,
PYTHONMALLOC) - onto these YAMLs.

The function never raises - I/O on the YAML is wrapped defensively so
``varis build my.yml`` can fail with a single typer.Exit(2) instead of
a Python traceback.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

import yaml

if TYPE_CHECKING:
    from pathlib import Path

# Repo-name substrings that identify each BSP. Order matters within a
# family: the first matching substring wins. The lists are kept short
# and explicit so a future BSP addition does not collide silently.
_NXP_REPO_NAMES: tuple[str, ...] = (
    "meta-imx",
    "meta-freescale",
    "meta-nxp",
    "meta-variscite-bsp-imx",
    "meta-variscite-sdk-imx",
)

_TI_REPO_NAMES: tuple[str, ...] = (
    "meta-ti-bsp",
    "meta-ti-extras",
    "meta-ti",
    "meta-tisdk",
    "meta-arago",
    "meta-variscite-bsp-ti",
    "meta-variscite-sdk-ti",
)


def _machine_family(machine: str) -> Literal["nxp", "ti", "unknown"]:
    if not machine:
        return "unknown"
    name = machine.lower()
    if name.startswith("imx"):
        return "nxp"
    if name.startswith(("am", "k3-", "j7-", "j72", "j78", "j784")):
        return "ti"
    return "unknown"


def _repos_family(repos: dict[str, Any] | None) -> Literal["nxp", "ti", "unknown"]:
    if not isinstance(repos, dict):
        return "unknown"
    names = set(repos.keys())
    for hit in _NXP_REPO_NAMES:
        if hit in names:
            return "nxp"
    for hit in _TI_REPO_NAMES:
        if hit in names:
            return "ti"
    return "unknown"


def detect_bsp_from_yaml(yaml_path: Path) -> Literal["nxp", "ti", "generic", "unknown"]:
    """Inspect a kas YAML and classify the BSP family.

    Pure function over a parsed YAML dict. Returns ``"generic"`` for a
    kas YAML that parses cleanly but lacks Variscite markers; callers
    use that to layer the BSP-agnostic tuning overlay. Returns
    ``"unknown"`` only for unparseable, empty, or shape-incomplete
    YAMLs - those exit with a typer.Exit(2) and a hint.
    """
    if yaml_path is None or not yaml_path.is_file():
        return "unknown"
    try:
        with yaml_path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError):
        return "unknown"
    if not isinstance(data, dict):
        return "unknown"

    machine = data.get("machine", "")
    machine_hit = _machine_family(machine if isinstance(machine, str) else "")
    if machine_hit != "unknown":
        return machine_hit

    repos_hit = _repos_family(data.get("repos"))
    if repos_hit != "unknown":
        return repos_hit

    # No NXP/TI markers but the YAML has at least a machine string or a
    # repos block - treat as a generic kas build. Reject only YAMLs
    # that lack both anchors (typo or empty file).
    has_machine = isinstance(machine, str) and bool(machine.strip())
    has_repos = isinstance(data.get("repos"), dict) and bool(data["repos"])
    if has_machine or has_repos:
        return "generic"
    return "unknown"
