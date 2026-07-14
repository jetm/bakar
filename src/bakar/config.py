"""Build configuration: defaults, env overrides, arg resolution.

The :class:`BuildConfig` carries everything `bakar build` needs to
dispatch a single run for either BSP family. ``bsp_family`` is fixed
at construction time (the dispatcher in cli.py inspects the manifest
filename or the user-supplied YAML and feeds the answer into
:func:`resolve`); every path property branches on that field so the
rest of bakar does not have to know about the workspace layout.
"""

from __future__ import annotations

import importlib.resources
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from bakar.preset_config import PresetEntry
    from bakar.user_config import UserConfig
    from bakar.workspace_config import WorkspaceConfig


def _overlay_dir() -> Path:
    """Locate the ``overlays/`` package data directory.

    Uses ``importlib.resources`` so the lookup works for both editable installs
    (source tree) and wheel installs (site-packages). ``uv_build`` includes all
    non-``.py`` files under ``src/bakar/`` automatically, so the YAMLs land at
    ``bakar/overlays/`` in the wheel. Lives here (foundation) rather than in
    ``commands/_helpers`` so ``steps`` can import it without a steps->commands edge.
    """
    return Path(str(importlib.resources.files("bakar") / "overlays"))


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


def pick(
    arg: str | None,
    env_key: str,
    ws_val: str | None,
    preset_val: str | None = None,
    user_val: str | None = None,
    default: str = "",
) -> str:
    if arg is not None:
        return arg
    env_val = os.environ.get(env_key)
    if env_val:
        return env_val
    if ws_val is not None:
        return ws_val
    if preset_val is not None:
        return preset_val
    if user_val is not None:
        return user_val
    return default


def pick_bool(env_key: str, *, ws_val: bool | None, user_val: bool) -> bool:
    """Resolve a boolean knob with precedence env > workspace > global.

    Mirrors :func:`pick`'s ordering for the bool tier used by ``rm_work`` and
    ``ccache``: a set ``BAKAR_*`` env var wins (``1/true/yes/on`` truthy, else
    falsy), then the workspace ``.bakar.toml`` value when not ``None`` (its
    "unset" sentinel), then the global config value (``UserConfig`` always
    carries a concrete bool - its built-in default when the key is absent).
    """
    env_val = os.environ.get(env_key)
    if env_val is not None and env_val.strip() != "":
        return env_val.strip().lower() in ("1", "true", "yes", "on")
    if ws_val is not None:
        return ws_val
    return user_val


def pick_host_toggle(
    env_key: str,
    *,
    ws_val: bool | None,
    user_val: bool | None,
) -> bool | None:
    """Resolve the explicit ``host_mode`` toggle, tri-state.

    Mirrors :func:`pick_bool`'s env > workspace > user ordering, but returns
    ``None`` when no tier explicitly sets the toggle so the caller can fall
    through to container auto-detection. A set ``BAKAR_HOST_MODE`` env var wins
    (``1/true/yes/on`` truthy, else falsy); then the workspace ``.bakar.toml``
    value when not ``None`` (its "unset" sentinel); then the user config value
    when not ``None``. The CLI ``--host`` flag is applied above this helper.
    """
    env_val = os.environ.get(env_key)
    if env_val is not None and env_val.strip() != "":
        return env_val.strip().lower() in ("1", "true", "yes", "on")
    if ws_val is not None:
        return ws_val
    if user_val is not None:
        return user_val
    return None


def _resolve_mold(
    *,
    user_config: UserConfig | None,
) -> tuple[bool, Literal["list", "global", "baseline"]]:
    """Resolve the mold enable toggle and mode at the accelerator tier.

    Precedence for the enable bool is ``BAKAR_MOLD`` env > ``[build] mold``
    config > default off. The mode is always ``list`` at this tier - the CLI
    ``--mold`` / ``--mold-baseline`` overrides (and the baseline mode they
    select) are applied above ``resolve()`` via
    :func:`bakar.commands._helpers.apply_mold_overrides`, mirroring how the
    global ``--sccache-dist`` flag is folded in after resolution.
    """
    resolved = pick_bool(
        "BAKAR_MOLD",
        ws_val=None,
        user_val=user_config.mold if user_config is not None else False,
    )
    return resolved, "list"


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
    kas_container_image: str
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
    # Abort the build when every running task's log has been silent this many
    # seconds (a wedged task). 0 disables the stall guard. Read by
    # _run_pty_with_ui's watchdog in steps/kas_build.py.
    stall_abort_secs: int = 2700
    # `bakar stop`'s graceful SIGINT wait auto-escalates to SIGTERM->SIGKILL
    # after this many seconds. The 30s default bounds the wait so a wedged
    # cooker (dead client fds holding the server open) cannot deadlock `bakar
    # stop` when no operator is present to press Ctrl-C; set to 0 to restore the
    # unbounded wait. Read by commands/stop.py, overridable per-invocation via
    # `bakar stop --timeout`.
    stop_grace_seconds: int = 30
    # SIGINT the build as soon as any task fails, instead of waiting for every
    # already-running task to drain on its own schedule. Read by
    # _run_pty_with_ui's error watchdog in steps/kas_build.py.
    stop_on_error: bool = True
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
    # When True, `bakar build` samples /proc/pressure during the build and writes
    # the recommended pressure_max_* back to config.toml.
    psi_autocalibrate: bool = field(default=False)
    sstate_mirror_url: str | None = field(default=None)
    # Decoupled build parallelism sourced from config.toml [build]. All None ->
    # the existing NPROC-derived behavior. _build_env() exports NPROC from nproc
    # (cpu_count fallback when None), and BAKAR_PARALLEL_MAKE / BAKAR_BB_NUMBER_THREADS
    # only when their override is set.
    nproc: int | None = field(default=None)
    parallel_make: int | None = field(default=None)
    bb_number_threads: int | None = field(default=None)
    # Distributed-compile via sccache-dist. When sccache_dist is True the tuning
    # stack swaps OE's compiler launcher to sccache and the client points at
    # sccache_scheduler_url. Both default to their unset values, so a build is
    # byte-for-byte unchanged (ccache stays) until the user opts in.
    sccache_dist: bool = field(default=False)
    sccache_scheduler_url: str | None = field(default=None)
    # mold linker. When mold is True the tuning stack adds the meta-bakar-mold
    # layer and inherits mold.bbclass, injecting -fuse-ld=mold into target link
    # steps. mold_mode selects the gate: "list" (allow-list via MOLD_INCLUDED_PN,
    # the default), "global" (deny-list via MOLD_EXCLUDED_PN), or "baseline"
    # (inject -fuse-ld=bfd over the same included set for symmetric measurement).
    # Default off, so a build is byte-for-byte unchanged until the user opts in.
    mold: bool = field(default=False)
    mold_mode: Literal["list", "global", "baseline"] = "list"
    # Bind address for the workspace cache services (hashserv, prserv). None
    # means localhost-only (single-node default); set to a cluster-reachable IP
    # so other nodes can share one hashserv/prserv. See user_config.cluster_bind_host.
    cluster_bind_host: str | None = field(default=None)
    # Central cross-node tier endpoints (host:port), provisioned by `bakar setup`
    # (CentralTierAction). When set, the build points BB_HASHSERVE / PRSERV_HOST at
    # the shared Rust/PostgreSQL services and skips the per-workspace bitbake
    # daemons; None keeps the per-workspace daemon path. See user_config.bb_hashserve.
    bb_hashserve: str | None = field(default=None)
    prserv_host: str | None = field(default=None)
    # Explicit cluster-mode opt-in (default off); the single gating signal for the
    # cluster preflight checks. See user_config.cluster.
    cluster: bool = field(default=False)
    # ccache enable toggle (default on). ccache and sccache co-exist as a hybrid:
    # the ccache overlay is selected whenever this flag is on (INCLUDING under
    # sccache_dist), so ccache is the local object cache for the non-allowlisted
    # recipe tail while sccache distributes the allowlisted heavy recipes.
    # use_ccache stays the parallelism-dominant-launcher marker (this flag AND NOT
    # sccache_dist). Set ccache=False to disable ccache outright.
    ccache: bool = field(default=True)
    # When False (the default while bakar is in use) the tuning stack strips
    # rm_work from both INHERIT and USER_CLASSES so recipe work dirs survive
    # (stone provisioning depends on previously-built native binaries). Set
    # rm_work=True to keep the container's default rm_work behavior.
    rm_work: bool = field(default=False)
    # When True, the live build UI loads persisted per-task timing baselines and
    # colors tasks that overrun their historical mean (drift). Default off so a
    # fresh checkout with no baseline history renders no misleading drift; opt in
    # via [build] show_baseline_drift / BAKAR_SHOW_BASELINE_DRIFT.
    show_baseline_drift: bool = field(default=False)
    # [host] doctor thresholds; defaults equal today's hardcoded literals in
    # diagnostics.py so verdicts are byte-identical until a value is written.
    # Resolved with precedence workspace .bakar.toml [host] > user config.toml [host] > default.
    host_inotify_instances: int = 4096
    host_inotify_watches: int = 524288
    host_swappiness_max: int = 20
    host_nofile_soft: int = 8192
    host_mem_min_gb: float = 16.0

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
    def hashserv_state_key(self) -> Path:
        """Directory that keys the persistent hashserv daemon (port, DB, PID).

        Returns the effective SSTATE_DIR so every workspace sharing one sstate
        cache shares one daemon and one hash-equivalence DB - the cache and its
        equivalence index stay paired instead of being rebuilt per workspace. A
        live ``SSTATE_DIR`` env var wins over the config value, matching the dir
        the build actually writes to (``_build_env`` exports it via setdefault).
        Falls back to ``bsp_root`` (today's per-workspace behavior) when no
        sstate dir is configured.
        """
        sstate = os.environ.get("SSTATE_DIR") or self.sstate_dir
        # Resolve to an absolute path: a relative SSTATE_DIR would otherwise make
        # the daemon's state dir (and the port derived from it) depend on the CWD
        # the CLI runs from, spawning duplicate daemons for one logical cache.
        return Path(sstate).resolve() if sstate else self.bsp_root

    @property
    def prserv_state_key(self) -> Path:
        """Directory that keys the persistent prserv daemon (port, DB).

        Shares the hashserv state key (the effective SSTATE_DIR) so the PR
        service DB lives with the same shared-cache lineage as sstate and the
        hash-equivalence DB. PRs then stay monotonic across every build and
        workspace that shares the cache instead of resetting to r0 when one
        build tree's volatile TMPDIR is wiped (the cause of the
        version-going-backwards setscene rejections).
        """
        return self.hashserv_state_key

    @property
    def use_shared_cache(self) -> bool:
        """True when an sstate mirror URL is configured."""
        return bool(self.sstate_mirror_url)

    @property
    def use_sccache_dist(self) -> bool:
        """True when distributed-compile via sccache-dist is enabled."""
        return bool(self.sccache_dist)

    @property
    def use_ccache(self) -> bool:
        """True when ccache is the dominant compile launcher for parallelism sizing.

        ccache and sccache both drive OE's CCACHE slot, but under the hybrid they
        co-exist (sccache for the allowlisted heavy recipes, ccache for the rest).
        This marks whether ccache is the *sole/dominant* launcher, so it stays
        False under sccache-dist - the launcher label feeds PARALLEL_MAKE sizing,
        which sccache-dist governs. Overlay selection uses the raw ``ccache``
        toggle instead (see ``_ccache_extra_overlays``), so the ccache overlay is
        still co-selected under sccache-dist.
        """
        return bool(self.ccache and not self.use_sccache_dist)

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
        except OSError:
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
    def bitbake_bin_path(self) -> Path:
        """Bundled bitbake ``bin`` directory for the host-mode launch PATH.

        Host builds launch bitbake via kas's
        ``find_program(ctx.environ['PATH'], 'bitbake')``, so this dir must be
        on the launch PATH or the launch fails. NXP/TI bundle bitbake under
        :attr:`bsp_bitbake_path` (``.../bitbake/bin``); the generic and bbsetup
        flows (including meta-avocado, whose YAML lives deep in the source tree
        but whose bitbake sits at the workspace top) take ``<workspace>/bitbake/bin``.
        """
        if self.bsp_family in ("ti", "nxp"):
            return self.bsp_bitbake_path / "bin"
        return self.workspace / "bitbake" / "bin"

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
        return pick(repo_branch, "BAKAR_REPO_BRANCH", None, None, None, fd.d_branch)
    if bsp_family == "ti":
        from bakar.bsp_model import infer_bsp_branch

        inferred = infer_bsp_branch(resolved_manifest)
        if inferred == "<unknown>":
            inferred = fd.d_branch
        return pick(repo_branch, "BAKAR_REPO_BRANCH", None, None, None, inferred)
    # nxp
    return pick(
        repo_branch,
        "BAKAR_REPO_BRANCH",
        None,
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
    # CLI --host: force the host path (back-compat alias; host is the default).
    host_mode: bool = False
    # CLI --container: opt into the kas-container path. Wins over host_mode.
    container_mode: bool = False


def compose_preset_output_path(preset: PresetEntry, release_index: int = 0) -> str:
    """Return a filesystem-safe output path suffix for a preset build.

    nxp/ti: ``<distro>-<machine>-<manifest_version>`` where manifest_version
    strips the BSP prefix up to the first digit group, e.g.
    ``imx-6.6.52-2.2.2.xml`` -> ``6.6.52-2.2.2``.

    bbsetup/generic single-release: ``<image>-<machine>``.
    bbsetup/generic multi-release: ``<image>-<machine>-<kas_yaml_stem>``.
    """
    if preset.family in {"nxp", "ti"}:
        manifest = preset.manifests[release_index] if preset.manifests else preset.manifest or ""
        stem = Path(manifest).stem
        m = re.search(r"(\d[\d.\-]+)", stem)
        version = m.group(1).rstrip("-") if m else stem
        parts = [p for p in [preset.distro, preset.machine] if p is not None]
        parts.append(version)
        return "-".join(parts)

    # bbsetup / generic
    if preset.kas_yamls:
        kas_stem = Path(preset.kas_yamls[release_index]).stem
        parts = [p for p in [preset.image, preset.machine] if p is not None]
        parts.append(kas_stem)
        return "-".join(parts)
    parts = [p for p in [preset.image, preset.machine] if p is not None]
    return "-".join(parts) if parts else "preset"


def resolve(
    *,
    workspace: Path,
    bsp_family: Literal["nxp", "ti", "generic", "bbsetup"] | None = None,
    spec: BSPSpec | None = None,
    kas_yaml: Path | None = None,
    user_config: UserConfig | None = None,
    workspace_config: WorkspaceConfig | None = None,
    preset: PresetEntry | None = None,
    family_is_explicit: bool = True,
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

    ``family_is_explicit`` (default ``True``) marks whether ``bsp_family``
    came from an explicit user flag. When a caller-supplied ``bsp_family``
    disagrees with an active ``preset``'s family, the default raises
    ``ValueError`` loudly. Pass ``family_is_explicit=False`` when
    ``bsp_family`` was instead derived via a heuristic fallback default -
    in that case a disagreement silently defers to the preset's family

    Mold is resolved from env/config only here (``BAKAR_MOLD`` env > ``[build]
    mold`` config > default off, always ``list`` mode); the CLI ``--mold`` /
    ``--mold-baseline`` overrides are folded into the returned config above
    ``resolve()`` via :func:`bakar.commands._helpers.apply_mold_overrides`.
    """

    if spec is None:
        spec = BSPSpec()

    # When the caller omits bsp_family (None), an active preset supplies the
    # family; otherwise fall back to "nxp". An explicitly-passed family is never
    # confused with "caller did not specify." When the caller DOES pass an
    # explicit, concrete family (nxp/ti) that disagrees with the active preset's
    # family, that is a genuine conflict (e.g. a TI preset invoked with an NXP
    # manifest) - raise loudly instead of silently keeping the explicit value.
    #
    # "generic" is excluded from the conflict check: it is the dispatch
    # classifier's fallback value for "not recognized as nxp/ti", not a
    # concretely-detected family. commands/build.py's BYO dispatch
    # (_dispatch_from_yaml) returns "generic" for any preset whose kas YAML
    # doesn't declare NXP/TI signals - including bbsetup presets, since
    # _dispatch_from_yaml's Literal return type has no "bbsetup" member. Per
    # specs/preset-build-resolution/spec.md's "bbsetup preset dispatches via
    # kas YAML" scenario, dispatch resolving to "generic" there is correct
    # behavior, not a conflict to reject.
    #
    # See the ``family_is_explicit`` docstring paragraph above for what the
    # not-explicit branch below does and why.
    if bsp_family is None:
        bsp_family = preset.family if preset is not None else "nxp"  # type: ignore[assignment]
    elif preset is not None and bsp_family in ("nxp", "ti") and bsp_family != preset.family:
        if not family_is_explicit:
            bsp_family = preset.family  # type: ignore[assignment]
        else:
            raise ValueError(
                f"bsp_family={bsp_family!r} conflicts with preset {preset.name!r}'s family={preset.family!r}"
            )

    # Thread preset branch into spec.repo_branch.  BSPSpec is frozen so we
    # create a replacement only when the caller left repo_branch unset.
    if preset is not None and spec.repo_branch is None and preset.branch is not None:
        from dataclasses import replace as _dc_replace

        spec = _dc_replace(spec, repo_branch=preset.branch)

    if workspace_config is None:
        # Lazy import keeps module load order flexible and avoids a hard
        # import cycle; centralizing the load here means every existing
        # caller of resolve() picks up the workspace tier for free.
        from bakar.workspace_config import load_workspace_config

        workspace_config = load_workspace_config(workspace)

    fd = _family_defaults(bsp_family, user_config, workspace_config)

    resolved_manifest = pick(
        spec.manifest,
        "BAKAR_MANIFEST",
        fd.ws_manifest,
        preset.manifest if preset is not None else None,
        fd.u_manifest,
        fd.d_manifest,
    )
    resolved_branch = _resolve_branch(bsp_family, fd, spec.repo_branch, resolved_manifest)

    # Host is the structural default; the kas-container path is opt-in. A
    # configured KAS_CONTAINER_IMAGE no longer auto-selects the container - only
    # an explicit container opt-in does (mirroring how --host used to be the
    # opt-in for the host path). Explicit container toggle resolved env >
    # workspace [build] container > user config container; None means no tier
    # set it, so the default (host) stands.
    explicit_container_toggle = pick_host_toggle(
        "BAKAR_CONTAINER",
        ws_val=workspace_config.container if workspace_config is not None else None,
        user_val=user_config.container if user_config is not None else None,
    )
    # Precedence (highest first): CLI --container (spec.container_mode) > CLI
    # --host (spec.host_mode, back-compat alias forcing host) > explicit
    # container toggle (env/workspace/user) > host (structural default). The
    # retained host_mode toggle is a no-op: host is already the default, so a
    # config carrying host_mode keeps working without selecting the container.
    if spec.container_mode:
        effective_host_mode = False
    elif spec.host_mode:
        effective_host_mode = True
    elif explicit_container_toggle is not None:
        effective_host_mode = not explicit_container_toggle
    else:
        effective_host_mode = True

    def _host(field_name: str, default: float) -> float:
        """Select a host threshold: workspace [host] > user [host] > built-in default.

        Mirrors pick()'s ws-before-user-before-default ordering for the numeric
        host_* fields. The workspace tier is the only sentinel-bearing source
        (its host_* fields are None when [host] is absent); UserConfig always
        carries the literal default, so user_val is never None when present.
        """
        ws_val = getattr(workspace_config, field_name) if workspace_config is not None else None
        if ws_val is not None:
            return ws_val
        user_val = getattr(user_config, field_name) if user_config is not None else None
        if user_val is not None:
            return user_val
        return default

    resolved_mold, resolved_mold_mode = _resolve_mold(user_config=user_config)

    return BuildConfig(
        workspace=workspace.resolve(),
        bsp_family=bsp_family,
        machine=pick(
            spec.machine,
            "BAKAR_MACHINE",
            fd.ws_machine,
            preset.machine if preset is not None else None,
            fd.u_machine,
            fd.d_machine,
        ),
        distro=pick(
            spec.distro,
            "BAKAR_DISTRO",
            fd.ws_distro,
            preset.distro if preset is not None else None,
            fd.u_distro,
            fd.d_distro,
        ),
        image=pick(
            spec.image,
            "BAKAR_IMAGE",
            fd.ws_image,
            preset.image if preset is not None else None,
            fd.u_image,
            fd.d_image,
        ),
        manifest=resolved_manifest,
        repo_url=os.environ.get(
            "BAKAR_REPO_URL",
            (user_config.nxp_repo_url if user_config and user_config.nxp_repo_url else DEFAULT_REPO_URL),
        ),
        repo_branch=resolved_branch,
        kas_container_image=pick(
            None,
            "KAS_CONTAINER_IMAGE",
            workspace_config.kas_container_image if workspace_config is not None else None,
            None,
            user_config.kas_container_image if user_config and user_config.kas_container_image else None,
            DEFAULT_CONTAINER_IMAGE,
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
        stall_abort_secs=user_config.stall_abort_secs if user_config else 2700,
        stop_grace_seconds=user_config.stop_grace_seconds if user_config else 30,
        stop_on_error=user_config.stop_on_error if user_config else True,
        use_hashequiv=user_config.hashserv if user_config else False,
        ccache_shared=user_config.ccache_shared if user_config else False,
        ccache_dir=user_config.ccache_dir if user_config else None,
        psi_autocalibrate=user_config.psi_autocalibrate if user_config else False,
        sstate_mirror_url=user_config.sstate_mirror_url if user_config else None,
        sccache_dist=pick_bool(
            "BAKAR_SCCACHE_DIST",
            ws_val=None,
            user_val=user_config.sccache_dist if user_config is not None else False,
        ),
        sccache_scheduler_url=user_config.sccache_scheduler_url if user_config else None,
        mold=resolved_mold,
        mold_mode=resolved_mold_mode,
        cluster_bind_host=user_config.cluster_bind_host if user_config else None,
        bb_hashserve=user_config.bb_hashserve if user_config else None,
        prserv_host=user_config.prserv_host if user_config else None,
        cluster=pick_bool(
            "BAKAR_CLUSTER",
            ws_val=None,
            user_val=user_config.cluster if user_config is not None else False,
        ),
        ccache=pick_bool(
            "BAKAR_CCACHE",
            ws_val=workspace_config.ccache if workspace_config is not None else None,
            user_val=user_config.ccache if user_config is not None else True,
        ),
        rm_work=pick_bool(
            "BAKAR_RM_WORK",
            ws_val=workspace_config.rm_work if workspace_config is not None else None,
            user_val=user_config.rm_work if user_config is not None else False,
        ),
        show_baseline_drift=pick_bool(
            "BAKAR_SHOW_BASELINE_DRIFT",
            ws_val=workspace_config.show_baseline_drift if workspace_config is not None else None,
            user_val=user_config.show_baseline_drift if user_config is not None else False,
        ),
        nproc=user_config.nproc if user_config else None,
        parallel_make=user_config.parallel_make if user_config else None,
        bb_number_threads=user_config.bb_number_threads if user_config else None,
        host_inotify_instances=int(_host("host_inotify_instances", 4096)),
        host_inotify_watches=int(_host("host_inotify_watches", 524288)),
        host_swappiness_max=int(_host("host_swappiness_max", 20)),
        host_nofile_soft=int(_host("host_nofile_soft", 8192)),
        host_mem_min_gb=float(_host("host_mem_min_gb", 16.0)),
    )
