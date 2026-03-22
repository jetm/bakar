"""Build configuration: defaults, env overrides, arg resolution.

The :class:`BuildConfig` carries everything `varis build` needs to
dispatch a single run for either BSP family. ``bsp_family`` is fixed
at construction time (the dispatcher in cli.py inspects the manifest
filename or the user-supplied YAML and feeds the answer into
:func:`resolve`); every path property branches on that field so the
rest of varis_build does not have to know about the workspace layout.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# NXP defaults (i.MX BSP, scarthgap warmup machine from
# kb/runbooks/first-bsp-build.md). Any of these can be overridden by
# VARIS_* env vars; CLI flags override env.
# ---------------------------------------------------------------------------

DEFAULT_NXP_MACHINE = "imx8mp-var-dart"
DEFAULT_NXP_DISTRO = "fsl-imx-xwayland"
DEFAULT_NXP_IMAGE = "core-image-minimal"
DEFAULT_NXP_MANIFEST = "imx-6.6.52-2.2.2.xml"
DEFAULT_NXP_REPO_BRANCH = "scarthgap"

# ---------------------------------------------------------------------------
# TI defaults (Sitara AM62x SoM, scarthgap-based Arago SDK 11.x).
# Pinned to the newest Variscite-shipped TI BSP at task creation time;
# bumped when a new processor-sdk-*-config_var<N>.txt config lands.
# ---------------------------------------------------------------------------

DEFAULT_TI_MACHINE = "am62x-var-som"
DEFAULT_TI_DISTRO = "arago"
DEFAULT_TI_IMAGE = "var-thin-image"
DEFAULT_TI_MANIFEST = "processor-sdk-scarthgap-chromium-11.00.09.04-config_var01.txt"
DEFAULT_TI_REPO_BRANCH = "scarthgap_11.00.09.04_var01"

# Back-compat aliases for any caller importing the pre-TI names.
DEFAULT_MACHINE = DEFAULT_NXP_MACHINE
DEFAULT_DISTRO = DEFAULT_NXP_DISTRO
DEFAULT_IMAGE = DEFAULT_NXP_IMAGE
DEFAULT_MANIFEST = DEFAULT_NXP_MANIFEST
DEFAULT_REPO_BRANCH = DEFAULT_NXP_REPO_BRANCH

DEFAULT_REPO_URL = "https://github.com/varigit/variscite-bsp-platform.git"
DEFAULT_CONTAINER_IMAGE = "jetm/kas-build-env:latest"

# NXP kernel -> variscite-bsp-platform branch mapping. The manifest XML
# files only exist on the branch they were authored for (imx-6.12.*.xml
# lives on walnascar, imx-6.6.*.xml on scarthgap), so `repo init -b`
# must match or the init step fails with "manifest file does not exist".
BRANCH_BY_MANIFEST_PREFIX: dict[str, str] = {
    "imx-6.6.": "scarthgap",
    "imx-6.12.": "walnascar",
}


def infer_repo_branch(manifest: str, fallback: str = DEFAULT_NXP_REPO_BRANCH) -> str:
    """Return the variscite-bsp-platform branch that carries this manifest."""
    for prefix, branch in BRANCH_BY_MANIFEST_PREFIX.items():
        if manifest.startswith(prefix):
            return branch
    return fallback


@dataclass(frozen=True)
class BuildConfig:
    """Resolved settings for a single `varis build` run.

    The BYO ``varis build my.yml`` flow sets ``kas_yaml_override`` to
    the user-supplied path; the manifest-driven flow leaves it None and
    falls back to ``default_kas_yaml``.
    """

    workspace: Path
    bsp_family: Literal["nxp", "ti", "generic"]
    machine: str
    distro: str
    image: str
    manifest: str
    repo_url: str
    repo_branch: str
    container_image: str
    # When True, kas-container is bypassed and plain `kas shell` is invoked
    # directly on the host. Used by the VARIS-18 Round 8 probe to rule out
    # kas-container/Docker as the parser-fork-race environment.
    host_mode: bool = False
    kas_yaml_override: Path | None = field(default=None)

    @property
    def workspace_subdir(self) -> str:
        """``"nxp"`` or ``"ti"`` - the BSP namespace under workspace root."""
        return self.bsp_family

    @property
    def bsp_root(self) -> Path:
        """Effective BSP root directory.

        For NXP and TI this is ``workspace/<bsp_family>/`` - the
        per-BSP namespace varis manages. Generic mode (BYO with no
        Variscite markers) does not own a workspace subdirectory; the
        user's YAML lives wherever they put it, so ``bsp_root`` falls
        back to the YAML's parent directory. That's where the overlay
        symlink and per-run state land for a generic build.
        """
        if self.bsp_family == "generic" and self.kas_yaml_override is not None:
            return self.kas_yaml_override.parent
        return self.workspace / self.bsp_family

    @property
    def bsp_bitbake_path(self) -> Path:
        """BSP-bundled bitbake directory swapped by the override step.

        NXP ships bitbake under the poky umbrella at
        ``nxp/sources/poky/bitbake/``. TI consumes oe-core directly and
        ships bitbake at the top of ``sources/`` as
        ``ti/sources/bitbake/``.
        """
        if self.bsp_family == "ti":
            return self.bsp_root / "sources" / "bitbake"
        return self.bsp_root / "sources" / "poky" / "bitbake"

    @property
    def bsp_bitbake_conf(self) -> Path:
        """BSP-bundled ``bitbake.conf`` consumed by the parser-compat check.

        NXP reads it from poky's meta layer; TI reads it from oe-core
        directly (no poky umbrella).
        """
        if self.bsp_family == "ti":
            return self.bsp_root / "sources" / "oe-core" / "meta" / "conf" / "bitbake.conf"
        return self.bsp_root / "sources" / "poky" / "meta" / "conf" / "bitbake.conf"

    @property
    def manifest_path(self) -> Path:
        """Absolute path to the manifest the dispatched step will consume.

        For NXP this is ``nxp/.repo/manifests/<m>.xml`` (managed by
        Google ``repo``); reading from there keeps varis aligned with
        what ``repo sync`` last produced.

        For TI this is ``ti/oe-layertool/configs/variscite/<m>.txt`` -
        the config file lives inside the cloned ``varigit/oe-layersetup``
        tree, not in a managed manifests dir.
        """
        if self.bsp_family == "ti":
            return self.bsp_root / "oe-layertool" / "configs" / "variscite" / self.manifest
        return self.bsp_root / ".repo" / "manifests" / self.manifest

    @property
    def bblayers_conf(self) -> Path:
        return self.bsp_root / "build" / "conf" / "bblayers.conf"

    @property
    def default_kas_yaml(self) -> Path:
        """Path the manifest-flow generator writes its output to.

        Lives under ``<bsp_root>/`` so kas-container - which mounts
        ``KAS_WORK_DIR`` (= ``bsp_root``) as ``/work`` - can read it
        without an extra bind mount.
        """
        return self.bsp_root / f"kas-{self.bsp_family}.yml"

    @property
    def kas_yaml(self) -> Path:
        """Effective kas YAML for this run.

        Returns ``kas_yaml_override`` when the user supplied one (BYO
        ``varis build my.yml``); otherwise the manifest-flow default.
        """
        return self.kas_yaml_override if self.kas_yaml_override is not None else self.default_kas_yaml

    @property
    def measurements_dir(self) -> Path:
        return self.bsp_root / "build" / "measurements"

    @property
    def runs_dir(self) -> Path:
        """Per-invocation run state (structured log, env snapshot, diagnostics)."""
        return self.bsp_root / "build" / "runs"


def resolve(
    *,
    workspace: Path,
    bsp_family: Literal["nxp", "ti", "generic"] = "nxp",
    machine: str | None = None,
    distro: str | None = None,
    image: str | None = None,
    manifest: str | None = None,
    repo_branch: str | None = None,
    host_mode: bool = False,
    kas_yaml: Path | None = None,
) -> BuildConfig:
    """Resolve BuildConfig from CLI flags, env vars, and family-specific defaults.

    Precedence, highest to lowest: explicit arg, VARIS_* env var,
    BSP-family default. ``repo_branch`` is special: when neither arg
    nor env is set, NXP infers it from the manifest filename via
    :data:`BRANCH_BY_MANIFEST_PREFIX`; TI infers it from the
    ``processor-sdk-<poky>-...-<sdk>-config_<var>`` regex via
    :func:`varis_build.bsp_model.infer_bsp_branch`.

    ``kas_yaml`` is the BYO override path. When set, it lands in
    :attr:`BuildConfig.kas_yaml_override` and ``cfg.kas_yaml`` returns
    it instead of the manifest-flow default. Mutually exclusive with
    ``manifest`` in practice; the dataclass is permissive so callers
    can build configs for tests without juggling exclusivity rules.

    ``bsp_family="generic"`` denotes a non-Variscite BYO build. The
    machine/distro/image/manifest fields all stay as inert
    placeholders since the manifest-flow pipeline never reads them
    in this mode - the user's kas YAML is the authoritative source.
    """

    def pick(arg: str | None, env_key: str, default: str) -> str:
        if arg is not None:
            return arg
        env_val = os.environ.get(env_key)
        if env_val:
            return env_val
        return default

    if bsp_family == "generic":
        d_machine, d_distro, d_image = "generic", "generic", "generic"
        d_manifest, d_branch = "", ""
    elif bsp_family == "ti":
        d_machine, d_distro, d_image = DEFAULT_TI_MACHINE, DEFAULT_TI_DISTRO, DEFAULT_TI_IMAGE
        d_manifest, d_branch = DEFAULT_TI_MANIFEST, DEFAULT_TI_REPO_BRANCH
    else:
        d_machine, d_distro, d_image = DEFAULT_NXP_MACHINE, DEFAULT_NXP_DISTRO, DEFAULT_NXP_IMAGE
        d_manifest, d_branch = DEFAULT_NXP_MANIFEST, DEFAULT_NXP_REPO_BRANCH

    resolved_manifest = pick(manifest, "VARIS_MANIFEST", d_manifest)

    if bsp_family == "generic":
        # No manifest, no branch inference - generic mode bypasses both.
        resolved_branch = pick(repo_branch, "VARIS_REPO_BRANCH", d_branch)
    elif bsp_family == "ti":
        # Lazy import: bsp_model has no cyclic deps on config.py, but
        # keeping the import inside resolve() keeps module import order
        # flexible.
        from varis_build.bsp_model import infer_bsp_branch

        inferred = infer_bsp_branch(resolved_manifest)
        if inferred == "<unknown>":
            inferred = d_branch
        resolved_branch = pick(repo_branch, "VARIS_REPO_BRANCH", inferred)
    else:
        resolved_branch = pick(
            repo_branch,
            "VARIS_REPO_BRANCH",
            infer_repo_branch(resolved_manifest, d_branch),
        )

    return BuildConfig(
        workspace=workspace.resolve(),
        bsp_family=bsp_family,
        machine=pick(machine, "VARIS_MACHINE", d_machine),
        distro=pick(distro, "VARIS_DISTRO", d_distro),
        image=pick(image, "VARIS_IMAGE", d_image),
        manifest=resolved_manifest,
        repo_url=os.environ.get("VARIS_REPO_URL", DEFAULT_REPO_URL),
        repo_branch=resolved_branch,
        container_image=os.environ.get("KAS_CONTAINER_IMAGE", DEFAULT_CONTAINER_IMAGE),
        host_mode=host_mode,
        kas_yaml_override=kas_yaml.resolve() if kas_yaml is not None else None,
    )
