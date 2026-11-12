"""Composite feed operations shared by ``bakar feed`` and ``bakar build --feed``.

Everything here was previously reachable only through the ``bakar feed`` command
module, which meant a second caller had to re-derive it. That is exactly what
happened when ``--feed`` was added: the deploy-dir derivation, the error
translation and the sync-then-index ordering were all reimplemented in
``commands/build.py``, and the copies had diverged before the first commit.

The split between this module and ``commands/feed.py`` is computation versus
presentation. What the deploy directory IS, what the prerequisites ARE, and what
a failure MEANS belong here, because both callers need identical answers. How
those are printed, and whether a failure exits non-zero, stay in the callers -
that is the one place they genuinely differ: ``bakar feed sync`` exits 1 because
syncing was the whole request, while ``--feed`` warns and leaves the build's exit
code alone because the build itself succeeded.

This module sits above ``feed``, ``feed_index`` and ``feed_preflight`` and below
the command layer. It imports no command module, so the command modules can
import it without a cycle.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from bakar import feed, feed_consolidate, feed_filter, feed_index, feed_lock, feed_preflight

if TYPE_CHECKING:
    from bakar.config import BuildConfig
    from bakar.diagnostics import CheckResult


def deploy_dir(cfg: BuildConfig) -> Path:
    """Return the RPM deploy directory the build wrote.

    ``avocado-repo.map`` sits here rather than one level up, and the map is what
    declares which repositories a sync renders - so this is the directory the
    feed stages from, not ``deploy`` itself.

    Derived from ``resolved_tmpdir`` rather than the workspace so a
    ``local_tmpdir_base`` override is honored: on the cluster the source tree is
    on NFS while the build TMPDIR is node-local, and staging from the workspace
    would find nothing.
    """
    return cfg.resolved_tmpdir / "deploy" / "rpm"


def declared_release_channel(cfg: BuildConfig) -> tuple[str, str] | tuple[None, None]:
    """Return the ``(release, channel)`` the finished build declares, or ``(None, None)``.

    Read through ``feed_consolidate.declared_release_channel``, the same reader
    the preflight cross-check uses. Local reads only.
    """
    return feed_consolidate.declared_release_channel(deploy_dir(cfg).parent)  # type: ignore[return-value]


def default_release_channel(cfg: BuildConfig, release: str | None, channel: str | None) -> tuple[str, str, bool]:
    """Fill an omitted release or channel from the build's declared codename.

    A given value is kept as given; the preflight cross-check then still blocks a
    pair the build disagrees with. Returns ``(release, channel, used_constants)``;
    ``used_constants`` is true when a value had to come from the module constants
    because the build declares no usable codename, so the caller can say so once.
    """
    if release is not None and channel is not None:
        return release, channel, False
    declared_release, declared_channel = declared_release_channel(cfg)
    if declared_release is None or declared_channel is None:
        return (
            feed.DEFAULT_RELEASE if release is None else release,
            feed.DEFAULT_CHANNEL if channel is None else channel,
            True,
        )
    return (
        declared_release if release is None else release,
        declared_channel if channel is None else channel,
        False,
    )


def scripts_dir(kas_yaml: Path) -> Path | None:
    """Return the meta-avocado scripts directory, or None when it is absent.

    Returning None rather than propagating lets a caller run the full preflight
    and report every missing prerequisite at once. The feed depends on a native
    binary and two shell tools that pip cannot install, so a first run on a fresh
    machine typically fails several checks - and surfacing them one exception at
    a time costs a round trip each.
    """
    try:
        return feed.meta_avocado_scripts(kas_yaml)
    except FileNotFoundError:
        return None


def preflight_results(
    cfg: BuildConfig | None,
    kas_yaml: Path | None = None,
    *,
    release: str | None = None,
    channel: str | None = None,
) -> list[CheckResult]:
    """Run every feed prerequisite check that the arguments make answerable.

    The argument assembly is the part worth sharing: which roots to test for
    writability, and that ``deploy_dir``/``scripts`` are only meaningful once a
    kas YAML names a build. A caller that got any of those wrong would check the
    wrong tree and report a pass over it.

    ``cfg`` is optional because the host-tool tier answers "can this machine
    build a feed at all" before any workspace exists - so a workspace that failed
    to resolve degrades the report rather than refusing it. ``release`` and
    ``channel`` are likewise optional: they enable the codename cross-check,
    which only a caller that knows where it intends to render can ask for.
    """
    has_build = cfg is not None and kas_yaml is not None
    roots = (
        feed.FeedRoots(feed_root=feed.resolve_feed_root(cfg), stage_root=feed.resolve_stage_root(cfg))
        if cfg is not None
        else None
    )
    build = feed_preflight.PreflightBuild(
        scripts=scripts_dir(kas_yaml) if has_build else None,
        deploy_dir=deploy_dir(cfg) if has_build else None,
        release=release,
        channel=channel,
    )
    return feed_preflight.preflight(roots=roots, build=build)


def describe_failure(exc: BaseException) -> str:
    """Return a human message for a sync failure, without a traceback.

    A ``CalledProcessError`` from the staging or render script carries the most
    important fact implicitly: the snapshot pointer is written last, so a script
    that exits non-zero leaves it untouched and the feed still serves the
    previous snapshot. Saying so is the actionable half - the raw repr says only
    that a long argv exited 1.
    """
    if isinstance(exc, subprocess.CalledProcessError):
        script = Path(exc.cmd[0]).name if exc.cmd else "a feed script"
        return (
            f"{script} exited {exc.returncode}. The snapshot pointer was not written, "
            "so the feed still serves the previous snapshot."
        )
    return str(exc)


@dataclass(frozen=True, kw_only=True, slots=True)
class SyncParams:
    """Optional knobs for :func:`sync_then_index`; the defaults reproduce the old behavior.

    ``only_packages`` is a set of RPM names the caller computed from the build
    output of the recipes it named. It is applied through a filtered hardlink
    view of the deploy dir, never read from ``recipe-map.json``, which is written
    after the publish.
    """

    feed_root: Path | None = None
    stage_root: Path | None = None
    only_packages: frozenset[str] | None = None
    snapshot: str | None = None
    index: bool = True


def sync_then_index(  # noqa: PLR0913 - existing keyword-only callers pass release/channel/sboms
    cfg: BuildConfig,
    kas_yaml: Path,
    *,
    release: str = feed.DEFAULT_RELEASE,
    channel: str = feed.DEFAULT_CHANNEL,
    sboms: list[Path] | None = None,
    params: SyncParams | None = None,
) -> dict[str, object]:
    """Stage a finished build, render what it declares, then rewrite the index.

    Index runs second and never alone: it derives the target list from what the
    channel has actually rendered, so indexing first would publish a
    ``targets.json`` describing the PREVIOUS sync while reporting success.

    The channel handed to the index comes from the sync's own result rather than
    being recomputed from ``cfg``. Recomputing agrees only while both use the
    same defaults; the moment a caller passes a non-default release the index
    would write - and ``write_targets_index`` MKDIRS its channel rather than
    refusing - a second, empty channel beside the one the sync just filled.

    ``params`` carries the optional roots, package subset, snapshot id and the
    index switch. Only the values it sets are forwarded to ``feed.sync``, so a
    default call hands it exactly what it received before ``params`` existed.

    Preconditions: the caller has already run :func:`preflight_results` and
    refused on a blocking result. This does not re-check, because the useful
    place to fail is before a multi-hour build rather than after it.
    """
    params = params or SyncParams()
    deploy = deploy_dir(cfg)
    view: Path | None = None
    optional = {
        "feed_root": params.feed_root,
        "stage_root": params.stage_root,
        "snapshot": params.snapshot,
    }
    # Locked once here for stage, render and index together: another writer
    # slipping in between them would index the wrong channel contents. The lock
    # follows the root the sync writes to, which a caller can override.
    lock_root = params.feed_root if params.feed_root is not None else feed.resolve_feed_root(cfg)
    try:
        with feed_lock.feed_lock(lock_root):
            if params.only_packages is not None:
                # Same parent as the deploy dir so the hardlinks stay on one filesystem.
                view = Path(tempfile.mkdtemp(prefix="feed-view-", dir=deploy.parent))
                deploy = feed_filter.build_filtered_view(deploy, view, params.only_packages)
            result = feed.sync(
                cfg,
                publish=feed.FeedPublish(
                    deploy_dir=deploy,
                    scripts=feed.meta_avocado_scripts(kas_yaml),
                    release=release,
                    channel=channel,
                    sboms=sboms,
                ),
                **{k: v for k, v in optional.items() if v is not None},
            )
            if params.index:
                result["index"] = feed_index.write_targets_index(Path(str(result["channel_root"])))
    finally:
        if view is not None:
            shutil.rmtree(view, ignore_errors=True)
    return result
