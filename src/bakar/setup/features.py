"""Feature catalog for ``bakar init``.

A static, declarative table of the fifteen optional build features init can
turn on. Each :class:`Feature` names the user-config keys it writes, the inputs
it asks for, how to detect it in an existing config (``markers``), how to turn
it off, and the :class:`Requirement` rows the host must satisfy.

This module imports only the standard library so the setup group's import
rules hold trivially; the catalog is data, and the init layer interprets it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Mapping

RequirementKind = Literal["check", "binary", "path", "setting", "probe"]
SettingKind = Literal["bool", "str", "int", "url", "path"]


def _frozen_map[V](items: Mapping[str, V] | None = None) -> Mapping[str, V]:
    return MappingProxyType(dict(items or {}))


@dataclass(frozen=True, slots=True)
class Requirement:
    """One thing the host must have for a feature to work."""

    kind: RequirementKind
    target: str
    hard: bool
    fix: str
    needs_values: bool = False
    skip_ok: bool = False
    host_only: bool = False


@dataclass(frozen=True, slots=True)
class FeatureSetting:
    """A written setting (dotted key) or an input (bare name, never written).

    An empty ``prompt`` means the value is never prompted; the init layer
    supplies it in ``values``.
    """

    key: str
    prompt: str
    kind: SettingKind
    default: str | None = None
    required: bool = False


@dataclass(frozen=True, slots=True)
class Feature:
    id: str
    tier: int
    title: str
    summary: str
    guide: str
    settings: tuple[FeatureSetting, ...] = ()
    inputs: tuple[FeatureSetting, ...] = ()
    fixed: Mapping[str, str] = field(default_factory=_frozen_map)
    # None unsets the key, a string resets it to that value.
    off: Mapping[str, str | None] = field(default_factory=_frozen_map)
    markers: tuple[str, ...] = ()
    marker_mode: Literal["all", "any"] = "all"
    default_on: bool = False
    requirements: tuple[Requirement, ...] = ()
    requires: tuple[str, ...] = ()
    excludes: tuple[str, ...] = ()
    warns_with: tuple[tuple[str, str], ...] = ()
    mode: Literal["any", "host"] = "any"
    persistent: bool = True


def _guide(feature_id: str) -> str:
    return f"docs/features/{feature_id}.md#what-your-host-needs"


_CENTRAL_GUIDE = "docs/features/central-tier.md#what-your-host-needs"

_SHARED_MOUNTS_FIX = (
    "set up shared mounts for the sstate and downloads directories, or "
    "select the nfs-cache feature (docs/features/nfs-cache.md)"
)

_CENTRAL_KEYS = ("build.bb_hashserve", "build.prserv_host", "build.cluster")

_HASHSERV_OVERRIDE = "the central hashserv endpoint overrides the per-workspace hashserv"


def _off_unset(*keys: str) -> Mapping[str, str | None]:
    return _frozen_map(dict.fromkeys(keys))


def _check(target: str, fix: str, *, hard: bool, **flags: bool) -> Requirement:
    return Requirement("check", target, hard, fix, **flags)


def _probe(target: str, fix: str, *, hard: bool, **flags: bool) -> Requirement:
    return Requirement("probe", target, hard, fix, **flags)


def _build_catalog() -> tuple[Feature, ...]:
    ccache = Feature(
        id="ccache",
        tier=1,
        title="Compiler cache",
        summary="Reuse compiled objects across builds with ccache.",
        guide=_guide("ccache"),
        settings=(
            FeatureSetting("build.ccache", "Enable ccache?", "bool", "true", required=True),
            FeatureSetting("build.ccache_shared", "Share the ccache across workspaces?", "bool", "false"),
            FeatureSetting("build.ccache_dir", "ccache directory", "path"),
        ),
        markers=("build.ccache",),
        off=_off_unset("build.ccache", "build.ccache_shared", "build.ccache_dir"),
        requirements=(
            Requirement("binary", "ccache", hard=True, fix="install ccache", host_only=True),
            _check(
                "ccache-health",
                "run `bakar doctor` and see the ccache section of the guide",
                hard=False,
                needs_values=True,
            ),
        ),
    )
    hashserv = Feature(
        id="hashserv",
        tier=1,
        title="Hash equivalence server",
        summary="Run a local bitbake hash equivalence server for sstate reuse.",
        guide=_guide("hashserv"),
        settings=(FeatureSetting("build.hashserv", "Enable hashserv?", "bool", "true", required=True),),
        markers=("build.hashserv",),
        off=_off_unset("build.hashserv"),
        requirements=(
            _check(
                "daemon-state",
                "point sstate_dir at local disk, or set build.bb_hashserve and build.prserv_host to the central tier",
                hard=True,
                needs_values=True,
                # SKIP means no per-workspace daemon is in use (a central endpoint
                # is set), so there is no state directory to keep off NFS.
                skip_ok=True,
            ),
            _check(
                "hashserv",
                "sync the workspace so bitbake-hashserv exists, then `bakar hashserv start`",
                hard=False,
                needs_values=True,
            ),
        ),
        warns_with=(
            ("central-node", _HASHSERV_OVERRIDE),
            ("central-client", _HASHSERV_OVERRIDE),
        ),
    )
    psi = Feature(
        id="psi",
        tier=1,
        title="PSI autocalibration",
        summary="Tune bitbake pressure thresholds from measured host pressure.",
        guide=_guide("psi"),
        settings=(
            FeatureSetting("build.psi_autocalibrate", "Autocalibrate PSI thresholds?", "bool", "true", required=True),
        ),
        markers=("build.psi_autocalibrate",),
        off=_off_unset("build.psi_autocalibrate"),
        requirements=(
            # A probe rather than the psi_support check: that check SKIPs on a PSI
            # host with no thresholds yet, which is exactly the host enabling psi.
            _probe(
                "psi-kernel",
                "build the kernel with CONFIG_PSI=y or boot with psi=1",
                hard=True,
            ),
        ),
    )
    scope = Feature(
        id="scope",
        tier=1,
        title="systemd scope isolation",
        summary="Run builds in a transient systemd scope with resource limits.",
        guide=_guide("scope"),
        settings=(FeatureSetting("build.scope", "Run builds in a systemd scope?", "bool", "true", required=True),),
        markers=("build.scope",),
        default_on=True,
        off=_frozen_map({"build.scope": "false"}),
        requirements=(
            _check(
                "systemd-scope",
                "use a systemd user session (systemd-run --user must work)",
                hard=True,
                # Against the proposed config: the check SKIPs while build.scope is false,
                # which is the state a user re-enabling scope starts from.
                needs_values=True,
            ),
            _check(
                "cgroup-v2",
                "boot with the unified cgroup hierarchy (systemd.unified_cgroup_hierarchy=1)",
                hard=False,
            ),
        ),
    )
    nfs_cache = Feature(
        id="nfs-cache",
        tier=1,
        title="Shared sstate and downloads",
        summary="Point sstate and downloads at shared (for example NFS) directories.",
        guide=_guide("nfs-cache"),
        settings=(
            FeatureSetting("build.sstate_dir", "sstate directory", "path", required=True),
            FeatureSetting("build.dl_dir", "downloads directory", "path", required=True),
        ),
        markers=("build.sstate_dir", "build.dl_dir"),
        marker_mode="any",
        off=_off_unset("build.sstate_dir", "build.dl_dir"),
        requirements=(
            _check(
                "cache-mounts",
                "mount the shared directories and re-run `bakar doctor`",
                hard=True,
                needs_values=True,
            ),
        ),
    )
    uninative = Feature(
        id="uninative",
        tier=1,
        title="Uninative",
        summary="Use the uninative tarball so sstate is portable across host distros.",
        guide=_guide("uninative"),
        mode="host",
        settings=(FeatureSetting("build.uninative", "Enable uninative?", "bool", "true", required=True),),
        markers=("build.uninative",),
        off=_off_unset("build.uninative"),
        requirements=(
            Requirement(
                "path",
                "/usr/share/yocto-uninative/uninative.inc",
                hard=True,
                fix="install the yocto-uninative-tarball package",
                host_only=True,
            ),
            _check(
                "uninative-fragment",
                "reinstall or rebuild the yocto-uninative-tarball package "
                "(its generated fragment is malformed), or set build.uninative to false",
                hard=True,
                needs_values=True,
                host_only=True,
            ),
        ),
    )
    sstate_mirror = Feature(
        id="sstate-mirror",
        tier=2,
        title="sstate mirror",
        summary="Fetch prebuilt sstate objects from a mirror.",
        guide=_guide("sstate-mirror"),
        settings=(FeatureSetting("build.sstate_mirror_url", "sstate mirror URL", "url", required=True),),
        markers=("build.sstate_mirror_url",),
        off=_off_unset("build.sstate_mirror_url"),
        requirements=(
            _check(
                "sstate-mirror-reachable",
                "check the URL and your network, then re-run `bakar doctor`",
                hard=True,
                needs_values=True,
            ),
            _check(
                "sstate-mirror-shared-mount",
                "mount the shared sstate directory, or see the guide",
                hard=True,
                needs_values=True,
                skip_ok=True,
            ),
        ),
    )
    dl_mirror = Feature(
        id="dl-mirror",
        tier=2,
        title="Downloads mirror",
        summary="Fetch source tarballs from a mirror before upstream.",
        guide=_guide("dl-mirror"),
        settings=(FeatureSetting("build.dl_mirror_url", "Downloads mirror URL", "url", required=True),),
        markers=("build.dl_mirror_url",),
        off=_off_unset("build.dl_mirror_url"),
        requirements=(
            _check(
                "dl-mirror-reachable",
                "check the URL and your network, then re-run `bakar doctor`",
                hard=True,
                needs_values=True,
            ),
        ),
    )
    cache_push = Feature(
        id="cache-push",
        tier=2,
        title="Cache push",
        summary="Push finished sstate and downloads to a remote cache host.",
        guide=_guide("cache-push"),
        settings=(
            FeatureSetting(
                "build.cache_push_target",
                "Cache push target (user@host:/path)",
                "str",
                required=True,
            ),
        ),
        markers=("build.cache_push_target",),
        off=_off_unset("build.cache_push_target"),
        requirements=(
            Requirement("binary", "inotifywait", hard=True, fix="install inotify-tools"),
            Requirement(
                "path",
                "~/.ssh/bakar-cache-push",
                hard=True,
                fix="create the cache push key described in docs/features/cache-push.md#what-your-host-needs",
            ),
            Requirement(
                "setting",
                "build.dl_dir",
                hard=True,
                fix="set a downloads directory, for example through the nfs-cache feature",
                needs_values=True,
            ),
            _check(
                "cache-push-service",
                "enable the cache-push service, see the guide",
                hard=False,
                needs_values=True,
            ),
        ),
    )
    sccache_dist = Feature(
        id="sccache-dist",
        tier=2,
        title="Distributed sccache",
        summary="Distribute compiles through an sccache scheduler.",
        guide=_guide("sccache-dist"),
        settings=(
            FeatureSetting("build.sccache_dist", "Enable sccache-dist?", "bool", "true", required=True),
            FeatureSetting("build.sccache_scheduler_url", "sccache scheduler URL", "url", required=True),
        ),
        markers=("build.sccache_dist", "build.sccache_scheduler_url"),
        off=_off_unset("build.sccache_dist", "build.sccache_scheduler_url"),
        requirements=(
            Requirement("binary", "sccache", hard=True, fix="install sccache", host_only=True),
            _check(
                "sccache-dist",
                "check the scheduler URL, then re-run `bakar doctor`",
                hard=True,
                needs_values=True,
            ),
        ),
    )
    central_node = Feature(
        id="central-node",
        tier=2,
        title="Central node",
        summary="Run the shared hashserv and PR server for a cluster of builders.",
        guide=_CENTRAL_GUIDE,
        settings=(
            FeatureSetting("build.bb_hashserve", "", "str"),
            FeatureSetting("build.prserv_host", "", "str"),
        ),
        inputs=(
            FeatureSetting("bind_host", "Address the central services bind to", "str", required=True),
            FeatureSetting("pg_host", "Postgres host", "str", "localhost"),
            FeatureSetting("pg_port", "Postgres port", "int", "5432"),
        ),
        fixed=_frozen_map({"build.cluster": "true"}),
        markers=_CENTRAL_KEYS,
        off=_off_unset(*_CENTRAL_KEYS),
        requirements=(
            _probe("central-units", "install and start the central systemd units", hard=True),
            _probe(
                "central-postgres",
                "start Postgres and check the host and port",
                hard=True,
                needs_values=True,
            ),
            _check(
                "shared-mounts",
                _SHARED_MOUNTS_FIX,
                hard=True,
                needs_values=True,
                skip_ok=True,
            ),
        ),
        excludes=("central-client",),
    )
    central_client = Feature(
        id="central-client",
        tier=2,
        title="Central client",
        summary="Use a central node's hashserv and PR server from this builder.",
        guide=_CENTRAL_GUIDE,
        settings=(
            FeatureSetting("build.bb_hashserve", "Central hashserv endpoint", "str", required=True),
            FeatureSetting("build.prserv_host", "Central PR server host:port", "str", required=True),
        ),
        fixed=_frozen_map({"build.cluster": "true"}),
        markers=_CENTRAL_KEYS,
        off=_off_unset(*_CENTRAL_KEYS),
        requirements=(
            _check(
                "central-hashserv",
                "check the endpoint and your network",
                hard=True,
                needs_values=True,
            ),
            _check(
                "central-prserv",
                "check the host:port and your network",
                hard=True,
                needs_values=True,
            ),
            _check(
                "shared-mounts",
                _SHARED_MOUNTS_FIX,
                hard=True,
                needs_values=True,
                skip_ok=True,
            ),
        ),
        excludes=("central-node",),
    )
    remote_dispatch = Feature(
        id="remote-dispatch",
        tier=3,
        title="Remote dispatch",
        summary="Dispatch builds to another host with --on.",
        guide=_guide("remote-dispatch"),
        persistent=False,
        settings=(
            FeatureSetting("dispatch.snapshot_untracked_cap_mb", "Untracked snapshot cap (MB)", "int"),
            FeatureSetting("dispatch.worktree_root", "Remote worktree root", "path"),
            FeatureSetting("dispatch.layer_repo_dir", "Remote layer repository directory", "path"),
        ),
        inputs=(
            FeatureSetting("host", "Remote host", "str", required=True),
            FeatureSetting("release", "Release workspace on the remote host", "str"),
        ),
        requires=("release-workspaces",),
        requirements=(
            _probe(
                "remote-host",
                "check ssh access to the host (`ssh <host> true`)",
                hard=True,
                needs_values=True,
            ),
            _probe(
                "remote-release-workspace",
                "create the release workspace on the remote host, "
                "see docs/features/remote-dispatch.md#what-your-host-needs",
                hard=False,
                needs_values=True,
            ),
        ),
        warns_with=(("feed", "a feed build cannot be dispatched with --on"),),
    )
    release_workspaces = Feature(
        id="release-workspaces",
        tier=3,
        title="Release workspaces",
        summary="Keep one workspace per release from a path template.",
        guide=_guide("release-workspaces"),
        settings=(FeatureSetting("workspaces.template", "Release workspace path template", "str", required=True),),
        inputs=(FeatureSetting("local_release", "Release to create locally", "str"),),
        markers=("workspaces.template",),
        off=_off_unset("workspaces.template"),
        requirements=(
            _probe(
                "local-release-workspace",
                "create the release workspace as described in docs/features/release-workspaces.md#what-your-host-needs",
                hard=False,
                needs_values=True,
            ),
        ),
    )
    feed = Feature(
        id="feed",
        tier=3,
        title="Package feed",
        summary="Publish built packages as a shared feed.",
        guide=_guide("feed"),
        settings=(
            FeatureSetting("build.feed_shared", "Share the feed?", "bool", "true"),
            FeatureSetting("build.feed_dir", "Feed directory", "path"),
        ),
        markers=("build.feed_shared", "build.feed_dir"),
        marker_mode="any",
        off=_off_unset("build.feed_shared", "build.feed_dir"),
        requirements=(
            _probe(
                "feed-host-tools",
                "install the feed host tools named in the guide",
                hard=True,
            ),
        ),
    )
    return (
        ccache,
        hashserv,
        psi,
        scope,
        nfs_cache,
        uninative,
        sstate_mirror,
        dl_mirror,
        cache_push,
        sccache_dist,
        central_node,
        central_client,
        remote_dispatch,
        release_workspaces,
        feed,
    )


CATALOG: tuple[Feature, ...] = _build_catalog()

FEATURE_IDS: tuple[str, ...] = tuple(f.id for f in CATALOG)

_BY_ID: Mapping[str, Feature] = MappingProxyType({f.id: f for f in CATALOG})


def feature_by_id(feature_id: str) -> Feature:
    """Return the feature named ``feature_id`` or raise ``KeyError`` listing every id."""
    try:
        return _BY_ID[feature_id]
    except KeyError:
        known = ", ".join(FEATURE_IDS)
        raise KeyError(f"unknown feature {feature_id!r}; known features: {known}") from None
