"""Per-BSP model for the bakar CLI.

NXP i.MX and TI Sitara BSP families are supported.
Each family has its own toolchain (Google ``repo`` + ``var-setup-release.sh``
for NXP; ``varigit/oe-layersetup`` shell wrapper for TI), its own
manifest format (``imx-A.B.C-X.Y.Z.xml`` vs ``processor-sdk-...-config_var<N>.txt``),
its own static tuning overlay (``overlays/bakar-tuning-<bsp>.yml``),
and its own pre-flight checks.

This module exports:

* :func:`detect_bsp_family` - classify a manifest filename. Pure regex,
  no I/O. The return value drives both the dispatcher in
  :mod:`bakar.cli` and the ``check_host_tools`` decision. For the
  complementary BYO-yaml classifier (content-based, not filename-based)
  see :func:`bakar.bsp_detect.detect_bsp_from_yaml`.
* :func:`infer_bsp_branch` - synthesize the
  ``meta-variscite-bsp-ti`` branch suffix from a TI config filename.
* :class:`BspModel` - dataclass + registry that hold every per-BSP
  knob: defaults, kas template, sync/setup steps, doctor extras.
* :func:`get_model` - factory returning the dispatched model. Imports
  are lazy so :mod:`bakar.config` (which imports
  :func:`infer_bsp_branch` for TI branch fallback) can keep its
  circular dep-free top-level import.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal

from bakar.vendor_config import load_vendors

if TYPE_CHECKING:
    from pathlib import Path

    from bakar.kas import KasTemplate


# ---------------------------------------------------------------------------
# Manifest-shape detection (no I/O on the regex path)
# ---------------------------------------------------------------------------


# NXP manifest filename: imx-<kernel>-<bsp>.xml
# Examples: imx-6.6.52-2.2.2.xml, imx-6.12.49-2.2.0.xml
_NXP_MANIFEST_RE = re.compile(r"^imx-\d+\.\d+\.\d+-\d+\.\d+\.\d+\.xml$")

# TI Processor SDK config filename:
#   processor-sdk-<poky>-<flavour>-<sdk>-config_<var>.txt
# where <poky> is the LTS code name (scarthgap, walnascar, ...),
# <flavour> is "chromium" / "non-chromium" / etc,
# <sdk> is a 4-part TI SDK version (11.00.09.04),
# <var>  is "var01" / "var02" / ...
_TI_PROCESSOR_SDK_RE = re.compile(
    r"^processor-sdk-"
    r"(?P<poky>[A-Za-z]\w*)-"
    r".*?-"
    r"(?P<sdk>\d+\.\d+\.\d+\.\d+)-"
    r"config_(?P<var>var\d+)\.txt$"
)

# TI legacy/Arago manifest filename: arago-<anything>.txt
# Kept as alternation for forward compatibility with older
# config naming conventions.
_TI_ARAGO_RE = re.compile(r"^arago-.*\.txt$")

# Layer name string used as a fallback heuristic when a config file is
# present but does not match either regex.
_TI_LAYER_PREFIX = "meta-variscite-bsp-ti"


def detect_bsp_family(
    manifest_path: Path | None,
    config_file: Path | None = None,
) -> Literal["nxp", "ti", "unknown"]:
    """Detect the BSP family from a manifest filename and optional config.

    Only the filename (``path.name``) is inspected; the file need not
    exist on disk. Pass ``None`` for either argument to skip that
    check. The function never raises - I/O on ``config_file`` is
    wrapped defensively.
    """
    if manifest_path is not None:
        name = manifest_path.name
        try:
            vendors = load_vendors()
        except ValueError:
            vendors = []
        for vendor in vendors:
            if re.match(vendor.manifest_regex, name):
                return vendor.family  # type: ignore[return-value]
        if _NXP_MANIFEST_RE.match(name):
            return "nxp"
        if _TI_PROCESSOR_SDK_RE.match(name) or _TI_ARAGO_RE.match(name):
            return "ti"

    if config_file is not None:
        try:
            text = config_file.read_text(encoding="utf-8", errors="replace")
            if _TI_LAYER_PREFIX in text:
                return "ti"
        except OSError:
            pass

    return "unknown"


def infer_bsp_branch(config_filename: str) -> str:
    """Derive the ``meta-variscite-bsp-ti`` branch from a TI config name.

    The canonical filename
    ``processor-sdk-scarthgap-chromium-11.00.09.04-config_var01.txt``
    parses to ``(poky=scarthgap, sdk=11.00.09.04, var=var01)`` and
    yields ``scarthgap_11.00.09.04_var01`` - the actual branch name on
    the layer repo.

    Returns ``"<unknown>"`` for inputs that do not match the regex
    (legacy ``arago-*.txt`` configs go through the legacy versioning
    scheme, which is out of scope for this helper).
    """
    m = _TI_PROCESSOR_SDK_RE.match(config_filename)
    if not m:
        return "<unknown>"
    return f"{m.group('poky')}_{m.group('sdk')}_{m.group('var')}"


# ---------------------------------------------------------------------------
# BspModel registry
# ---------------------------------------------------------------------------


# Type aliases. ``Callable[..., None]`` is intentionally permissive so
# both the NXP sync step (``init_and_sync(cfg, log, *, force_init=...)``)
# and the TI sync step (``populate(cfg, log, *, force_init=...)``) fit.
SyncStep = Callable[..., None]
SetupEnvStep = Callable[..., None]
DoctorCheck = Callable[..., Any]


@dataclass(frozen=True)
class BspModel:
    """Per-BSP knobs consumed by the dispatch layer.

    Every field that varies between NXP and TI lives here. The CLI's
    ``_dispatch_bsp`` returns one of two singleton instances; downstream
    code reads the fields instead of switching on ``cfg.bsp_family``.
    """

    family: Literal["nxp", "ti"]
    workspace_subdir: str  # "nxp" or "ti"
    kas_yaml_filename: str  # "kas-nxp.yml" or "kas-ti.yml"
    tuning_overlay_filename: str  # "bakar-tuning-nxp.yml" or "bakar-tuning-ti.yml"
    manifest_kind: Literal["repo-xml", "oe-layertool-config"]
    default_machine: str
    default_distro: str
    default_image: str
    default_manifest: str
    default_branch: str
    required_host_tools: tuple[str, ...]
    sync_step: SyncStep
    setup_env_step: SetupEnvStep
    kas_template: KasTemplate
    doctor_extras: tuple[DoctorCheck, ...]


def get_model(family: Literal["nxp", "ti"]) -> BspModel:
    """Return the BspModel singleton for ``family``.

    Imports are intentionally lazy. ``bsp_model`` is imported from
    ``config.py`` (``infer_bsp_branch``) and from ``cli.py``; deferring
    the heavy imports until ``get_model`` is actually called keeps the
    import graph clean and lets unit tests construct dummy models
    without dragging in diagnostics or steps.
    """
    # Lazy: avoid pulling diagnostics/steps/kas into every consumer of
    # detect_bsp_family or infer_bsp_branch.
    from bakar import config as cfg_mod
    from bakar.diagnostics import (
        check_forks_linux_imx,
        check_forks_ti_linux_kernel,
        check_forks_ti_u_boot,
        check_git_object_cache,
        check_manifest_consistency,
        check_ti_layertool_config_consistency,
        check_ti_layertool_present,
    )
    from bakar.kas import NXP_KAS_TEMPLATE, TI_KAS_TEMPLATE
    from bakar.steps import repo as step_repo
    from bakar.steps import setup_env as step_setup
    from bakar.steps import ti_layertool as step_ti_layertool
    from bakar.steps import ti_setup_env as step_ti_setup

    if family == "nxp":
        model = BspModel(
            family="nxp",
            workspace_subdir="nxp",
            kas_yaml_filename="kas-nxp.yml",
            tuning_overlay_filename="bakar-tuning-nxp.yml",
            manifest_kind="repo-xml",
            default_machine=cfg_mod.DEFAULT_NXP_MACHINE,
            default_distro=cfg_mod.DEFAULT_NXP_DISTRO,
            default_image=cfg_mod.DEFAULT_NXP_IMAGE,
            default_manifest=cfg_mod.DEFAULT_NXP_MANIFEST,
            default_branch=cfg_mod.DEFAULT_NXP_REPO_BRANCH,
            required_host_tools=("repo", "kas-container", "docker", "python3"),
            sync_step=step_repo.init_and_sync,
            setup_env_step=step_setup.run,
            kas_template=NXP_KAS_TEMPLATE,
            doctor_extras=(
                check_forks_linux_imx,
                check_manifest_consistency,
                check_git_object_cache,
            ),
        )
    elif family == "ti":
        model = BspModel(
            family="ti",
            workspace_subdir="ti",
            kas_yaml_filename="kas-ti.yml",
            tuning_overlay_filename="bakar-tuning-ti.yml",
            manifest_kind="oe-layertool-config",
            default_machine=cfg_mod.DEFAULT_TI_MACHINE,
            default_distro=cfg_mod.DEFAULT_TI_DISTRO,
            default_image=cfg_mod.DEFAULT_TI_IMAGE,
            default_manifest=cfg_mod.DEFAULT_TI_MANIFEST,
            default_branch=cfg_mod.DEFAULT_TI_REPO_BRANCH,
            required_host_tools=("git", "kas-container", "docker", "python3"),
            sync_step=step_ti_layertool.populate,
            setup_env_step=step_ti_setup.run,
            kas_template=TI_KAS_TEMPLATE,
            doctor_extras=(
                check_ti_layertool_present,
                check_ti_layertool_config_consistency,
                check_forks_ti_linux_kernel,
                check_forks_ti_u_boot,
            ),
        )
    else:
        raise ValueError(f"Unknown BSP family: {family!r}")

    # Apply optional VendorEntry overrides for the first matching vendor.
    for entry in load_vendors():
        if entry.family == family:
            overrides: dict[str, Any] = {}
            if entry.default_machine is not None:
                overrides["default_machine"] = entry.default_machine
            if entry.default_distro is not None:
                overrides["default_distro"] = entry.default_distro
            if entry.default_image is not None:
                overrides["default_image"] = entry.default_image
            if entry.default_manifest is not None:
                overrides["default_manifest"] = entry.default_manifest
            if entry.default_branch is not None:
                overrides["default_branch"] = entry.default_branch
            if entry.tuning_overlay is not None:
                overrides["tuning_overlay_filename"] = entry.tuning_overlay
            if overrides:
                model = replace(model, **overrides)
            break

    return model
