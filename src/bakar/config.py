"""Build configuration: defaults, env overrides, arg resolution.

The :class:`BuildConfig` carries everything `bakar build` needs to
dispatch a single run for either BSP family. ``bsp_family`` is fixed
at construction time (the dispatcher in cli.py inspects the manifest
filename or the user-supplied YAML and feeds the answer into
:func:`resolve`); every path property branches on that field so the
rest of bakar does not have to know about the workspace layout.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from bakar.user_config import UserConfig
    from bakar.workspace_config import WorkspaceConfig

# ---------------------------------------------------------------------------
# NXP defaults (i.MX BSP, scarthgap warmup machine).
# Any of these can be overridden by BAKAR_* env vars; CLI flags override env.
# ---------------------------------------------------------------------------

DEFAULT_NXP_MACHINE = "imx8mp-var-dart"
DEFAULT_NXP_DISTRO = "fsl-imx-xwayland"
DEFAULT_NXP_IMAGE = "core-image-minimal"
DEFAULT_NXP_MANIFEST = "imx-6.6.52-2.2.2.xml"
DEFAULT_NXP_REPO_BRANCH = "scarthgap"

# ---------------------------------------------------------------------------
# TI defaults (Sitara AM62x SoM, scarthgap-based Arago SDK 11.x).
# Pinned to the newest vendor-shipped TI BSP at task creation time;
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


def shared_ccache_dir(ccache_dir: str | None, *, ccache_shared: bool) -> Path | None:
    """Resolve a non-per-workspace ccache directory, or None for per-workspace.

    An explicit ``ccache_dir`` wins; otherwise ``ccache_shared`` selects a single
    cache under the XDG cache home (``~/.cache/bakar/ccache``). Returns None when
    neither is set, signalling the caller to use the per-workspace default. Shared
    by :attr:`BuildConfig.effective_ccache_dir` and the ``clean-cache`` command so
    both agree on where a shared cache lives.
    """
    if ccache_dir:
        return Path(ccache_dir).expanduser()
    if ccache_shared:
        cache_home = os.environ.get("XDG_CACHE_HOME")
        base = Path(cache_home) if cache_home else Path.home() / ".cache"
        return base / "bakar" / "ccache"
    return None


def pick(arg: str | None, env_key: str, ws_val: str | None, user_val: str | None, default: str) -> str:
    if arg is not None:
        return arg
    env_val = os.environ.get(env_key)
    if env_val:
        return env_val
    if ws_val is not None:
        return ws_val
    if user_val is not None:
        return user_val
    return default


@dataclass(frozen=True)
class _FamilyDefaults:
    d_machine: str
    d_distro: str
    d_image: str
    d_manifest: str
    d_branch: str
    u_machine: str | None
    u_distro: str | None
    u_image: str | None
    u_manifest: str | None
    ws_machine: str | None
    ws_distro: str | None
    ws_image: str | None
    ws_manifest: str | None


def _family_defaults(
    bsp_family: Literal["nxp", "ti", "generic", "bbsetup"],
    user_config: UserConfig | None,
    workspace_config: WorkspaceConfig,
) -> _FamilyDefaults:
    """Return per-family defaults, user-config, and workspace-config values."""
    if bsp_family in ("generic", "bbsetup"):
        return _FamilyDefaults(
            d_machine="generic",
            d_distro="generic",
            d_image="generic",
            d_manifest="",
            d_branch="",
            u_machine=None,
            u_distro=None,
            u_image=None,
            u_manifest=None,
            ws_machine=workspace_config.generic_machine if bsp_family == "generic" else None,
            ws_distro=None,
            ws_image=None,
            ws_manifest=None,
        )
    if bsp_family == "ti":
        return _FamilyDefaults(
            d_machine=DEFAULT_TI_MACHINE,
            d_distro=DEFAULT_TI_DISTRO,
            d_image=DEFAULT_TI_IMAGE,
            d_manifest=DEFAULT_TI_MANIFEST,
            d_branch=DEFAULT_TI_REPO_BRANCH,
            u_machine=user_config.ti_machine if user_config is not None else None,
            u_distro=user_config.ti_distro if user_config is not None else None,
            u_image=user_config.ti_image if user_config is not None else None,
            u_manifest=user_config.ti_manifest if user_config is not None else None,
            ws_machine=workspace_config.ti_machine if workspace_config is not None else None,
            ws_distro=workspace_config.ti_distro if workspace_config is not None else None,
            ws_image=workspace_config.ti_image if workspace_config is not None else None,
            ws_manifest=workspace_config.ti_manifest if workspace_config is not None else None,
        )
    # nxp (default)
    return _FamilyDefaults(
        d_machine=DEFAULT_NXP_MACHINE,
        d_distro=DEFAULT_NXP_DISTRO,
        d_image=DEFAULT_NXP_IMAGE,
        d_manifest=DEFAULT_NXP_MANIFEST,
        d_branch=DEFAULT_NXP_REPO_BRANCH,
        u_machine=user_config.nxp_machine if user_config is not None else None,
        u_distro=user_config.nxp_distro if user_config is not None else None,
        u_image=user_config.nxp_image if user_config is not None else None,
        u_manifest=user_config.nxp_manifest if user_config is not None else None,
        ws_machine=workspace_config.nxp_machine if workspace_config is not None else None,
        ws_distro=workspace_config.nxp_distro if workspace_config is not None else None,
        ws_image=workspace_config.nxp_image if workspace_config is not None else None,
        ws_manifest=workspace_config.nxp_manifest if workspace_config is not None else None,
    )


@dataclass(frozen=True)
class BuildConfig:
    """Resolved settings for a single `bakar build` run.

    The BYO ``bakar build my.yml`` flow sets ``kas_yaml_override`` to
    the user-supplied path; the manifest-driven flow leaves it None and
    falls back to ``default_kas_yaml``.
    """

    workspace: Path
    bsp_family: Literal["nxp", "ti", "generic", "bbsetup"]
    machine: str
    distro: str
    image: str
    manifest: str
    repo_url: str
    repo_branch: str
    container_image: str
    # When True, kas-container is bypassed and plain `kas shell` is invoked
    # directly on the host to rule out kas-container/Docker as the parser-fork-race environment.
    host_mode: bool = False
    kas_yaml_override: Path | None = field(default=None)
    # Build-tuning fields sourced from config.toml [build]. These carry the
    # user-supplied values down to _build_env() which emits them into the
    # kas-container environment; env-var precedence is applied there, not here.
    dl_dir: str | None = field(default=None)
    sstate_dir: str | None = field(default=None)
    sstate_mirrors: str | None = field(default=None)
    scheduler: str | None = field(default=None)
    pressure_max_cpu: float | None = field(default=None)
    pressure_max_io: float | None = field(default=None)
    pressure_max_memory: float | None = field(default=None)
    disk_free_threshold_gb: float = 50.0
    use_hashequiv: bool = field(default=False)
    # ccache location. Per-workspace by default; opt into a single shared cache
    # across all workspaces via [build] ccache_shared, or pin an explicit path
    # via [build] ccache_dir.
    #
    # NOTE: when ccache_shared is True the single shared cache is still governed
    # by the build cap (CCACHE_MAXSIZE = 50G, set in the tuning overlays). One
    # cache feeding many BSPs may evict under that cap; raise CCACHE_MAXSIZE in
    # the environment (it overrides the overlay) when sharing widely.
    ccache_shared: bool = field(default=False)
    ccache_dir: str | None = field(default=None)

    @property
    def effective_ccache_dir(self) -> Path:
        """Host directory bind-mounted to ``/work/ccache``.

        Per-workspace by default (``<workspace>/ccache``), so each BSP keeps an
        isolated cache. An explicit ``ccache_dir`` is honored verbatim (a shared
        location of the user's choosing); otherwise ``ccache_shared`` selects a
        single cache under the XDG cache home (``~/.cache/bakar/ccache``) that
        every workspace reuses. oe-core sets ``CCACHE_BASEDIR``/``CCACHE_NOHASHDIR``,
        so a shared cache yields cross-workspace hits without path-keyed misses.
        """
        return shared_ccache_dir(self.ccache_dir, ccache_shared=self.ccache_shared) or self.workspace / "ccache"

    @property
    def workspace_subdir(self) -> str:
        """``"nxp"`` or ``"ti"`` - the BSP namespace under workspace root."""
        return self.bsp_family

    @property
    def is_meta_avocado(self) -> bool:
        """True when the kas YAML lives inside a meta-avocado repository.

        Drives the ``init-build``-style build-directory setup in
        :mod:`bakar.steps.kas_build`: a build dir is created next to
        the ``meta-avocado/`` repo with a symlink back to it so kas can
        resolve all layer paths without cloning meta-avocado again.
        """
        if self.bsp_family != "generic" or self.kas_yaml_override is None:
            return False
        try:
            return "meta-avocado" in self.kas_yaml_override.resolve().parts
        except Exception:
            return False

    @property
    def bsp_root(self) -> Path:
        """Effective BSP root directory.

        For NXP and TI this is ``workspace/<bsp_family>/`` - the
        per-BSP namespace bakar manages. Generic mode (BYO with no
        NXP/TI markers) does not own a workspace subdirectory; the
        user's YAML lives wherever they put it, so ``bsp_root`` falls
        back to the YAML's parent directory. That's where the overlay
        symlink and per-run state land for a generic build.

        meta-avocado is the exception: its kas YAMLs live deep inside the
        ``meta-avocado/`` source tree, but kas must run from a dedicated
        build directory that is a sibling of that tree. For those builds
        ``bsp_root`` is ``workspace/build-<yaml-stem>``
        (e.g. ``sources/build-qemux86-64``), mirroring what the
        ``meta-avocado/scripts/init-build`` script produces.
        """
        if self.bsp_family == "generic" and self.kas_yaml_override is not None:
            if self.is_meta_avocado:
                return self.workspace / f"build-{self.kas_yaml_override.stem}"
            return self.kas_yaml_override.parent
        if self.bsp_family == "bbsetup":
            return self.workspace
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
        reading from there keeps bakar aligned with
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
        ``bakar build my.yml``); otherwise the manifest-flow default.
        """
        return self.kas_yaml_override if self.kas_yaml_override is not None else self.default_kas_yaml

    @property
    def measurements_dir(self) -> Path:
        return self.bsp_root / "build" / "measurements"

    @property
    def runs_dir(self) -> Path:
        """Per-invocation run state (structured log, env snapshot, diagnostics)."""
        return self.bsp_root / "build" / "runs"


def _resolve_branch(
    bsp_family: Literal["nxp", "ti", "generic", "bbsetup"],
    fd: _FamilyDefaults,
    repo_branch: str | None,
    resolved_manifest: str,
) -> str:
    """Resolve the effective repo branch from CLI flag, env, and BSP family inference."""
    if bsp_family in ("generic", "bbsetup"):
        return pick(repo_branch, "BAKAR_REPO_BRANCH", None, None, fd.d_branch)
    if bsp_family == "ti":
        from bakar.bsp_model import infer_bsp_branch

        inferred = infer_bsp_branch(resolved_manifest)
        if inferred == "<unknown>":
            inferred = fd.d_branch
        return pick(repo_branch, "BAKAR_REPO_BRANCH", None, None, inferred)
    # nxp
    return pick(
        repo_branch,
        "BAKAR_REPO_BRANCH",
        None,
        None,
        infer_repo_branch(resolved_manifest, fd.d_branch),
    )


@dataclass(slots=True, kw_only=True)
class BSPSpec:
    """BSP target fields passed to :func:`resolve`."""

    machine: str | None = None
    distro: str | None = None
    image: str | None = None
    manifest: str | None = None
    repo_branch: str | None = None
    host_mode: bool = False


def resolve(
    *,
    workspace: Path,
    bsp_family: Literal["nxp", "ti", "generic", "bbsetup"] = "nxp",
    spec: BSPSpec | None = None,
    kas_yaml: Path | None = None,
    user_config: UserConfig | None = None,
    workspace_config: WorkspaceConfig | None = None,
) -> BuildConfig:
    """Resolve BuildConfig from CLI flags, env vars, config, and family defaults.

    Precedence, highest to lowest:
    ``CLI flag > BAKAR_* env var > workspace .bakar.toml > user config.toml >
    built-in default``. ``workspace_config`` carries the
    ``[defaults.<family>]`` values from the workspace's ``.bakar.toml``; when
    ``None`` (the default) it is auto-loaded from ``workspace`` via
    :func:`load_workspace_config`, so every existing caller picks up the
    workspace tier with no signature change at the call site. ``user_config``
    carries the values loaded from ``~/.config/bakar/config.toml``; ``None``
    (the default) preserves the pre-config behavior for direct callers.
    ``spec`` is a :class:`BSPSpec` carrying the six BSP target fields
    (``machine``, ``distro``, ``image``, ``manifest``, ``repo_branch``,
    ``host_mode``). When ``None`` (the default), a :class:`BSPSpec` with all
    fields at their defaults is used. ``repo_branch`` is special: it has no
    config field, so when neither arg nor env is set, NXP infers it from the
    manifest filename via :data:`BRANCH_BY_MANIFEST_PREFIX`; TI infers it from
    the ``processor-sdk-<poky>-...-<sdk>-config_<var>`` regex via
    :func:`bakar.bsp_model.infer_bsp_branch`.

    ``kas_yaml`` is the BYO override path. When set, it lands in
    :attr:`BuildConfig.kas_yaml_override` and ``cfg.kas_yaml`` returns
    it instead of the manifest-flow default. Mutually exclusive with
    ``manifest`` in practice; the dataclass is permissive so callers
    can build configs for tests without juggling exclusivity rules.

    ``bsp_family="generic"`` denotes a generic BYO build. The
    machine/distro/image/manifest fields all stay as inert
    placeholders since the manifest-flow pipeline never reads them
    in this mode - the user's kas YAML is the authoritative source.

    ``bsp_family="bbsetup"`` behaves like generic for defaults (inert
    machine/distro/image placeholders, no manifest/branch inference);
    the real machine/distro come from the bitbake-setup config
    translation step, not from this resolver.
    """

    if spec is None:
        spec = BSPSpec()

    if workspace_config is None:
        # Lazy import keeps module load order flexible and avoids a hard
        # import cycle; centralizing the load here means every existing
        # caller of resolve() picks up the workspace tier for free.
        from bakar.workspace_config import load_workspace_config

        workspace_config = load_workspace_config(workspace)

    fd = _family_defaults(bsp_family, user_config, workspace_config)

    resolved_manifest = pick(spec.manifest, "BAKAR_MANIFEST", fd.ws_manifest, fd.u_manifest, fd.d_manifest)
    resolved_branch = _resolve_branch(bsp_family, fd, spec.repo_branch, resolved_manifest)

    # Auto-detect: when KAS_CONTAINER_IMAGE is absent from env and host_mode was
    # not explicitly requested, fall back to plain kas (no Docker) rather than the
    # hardcoded default container image. This makes bakar work out of the box on
    # hosts without a container setup. A config-supplied container_image counts the
    # same as the env var: a user who set it has a container setup and wants it used.
    effective_host_mode = spec.host_mode or (
        "KAS_CONTAINER_IMAGE" not in os.environ and (user_config is None or user_config.container_image is None)
    )

    return BuildConfig(
        workspace=workspace.resolve(),
        bsp_family=bsp_family,
        machine=pick(spec.machine, "BAKAR_MACHINE", fd.ws_machine, fd.u_machine, fd.d_machine),
        distro=pick(spec.distro, "BAKAR_DISTRO", fd.ws_distro, fd.u_distro, fd.d_distro),
        image=pick(spec.image, "BAKAR_IMAGE", fd.ws_image, fd.u_image, fd.d_image),
        manifest=resolved_manifest,
        repo_url=os.environ.get(
            "BAKAR_REPO_URL",
            (user_config.nxp_repo_url if user_config and user_config.nxp_repo_url else DEFAULT_REPO_URL),
        ),
        repo_branch=resolved_branch,
        container_image=os.environ.get(
            "KAS_CONTAINER_IMAGE",
            (user_config.container_image if user_config and user_config.container_image else DEFAULT_CONTAINER_IMAGE),
        ),
        host_mode=effective_host_mode,
        kas_yaml_override=kas_yaml.resolve() if kas_yaml is not None else None,
        dl_dir=user_config.dl_dir if user_config else None,
        sstate_dir=user_config.sstate_dir if user_config else None,
        sstate_mirrors=user_config.sstate_mirrors if user_config else None,
        scheduler=user_config.scheduler if user_config else None,
        pressure_max_cpu=user_config.pressure_max_cpu if user_config else None,
        pressure_max_io=user_config.pressure_max_io if user_config else None,
        pressure_max_memory=user_config.pressure_max_memory if user_config else None,
        disk_free_threshold_gb=user_config.disk_free_threshold_gb if user_config else 50.0,
        use_hashequiv=user_config.hashserv if user_config else False,
        ccache_shared=user_config.ccache_shared if user_config else False,
        ccache_dir=user_config.ccache_dir if user_config else None,
    )
