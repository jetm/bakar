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

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from bakar import feed, feed_index, feed_preflight

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
    return feed_preflight.preflight(
        feed_root=feed.resolve_feed_root(cfg) if cfg is not None else None,
        stage_root=feed.resolve_stage_root(cfg) if cfg is not None else None,
        scripts=scripts_dir(kas_yaml) if has_build else None,
        deploy_dir=deploy_dir(cfg) if has_build else None,
        release=release,
        channel=channel,
    )


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


def sync_then_index(
    cfg: BuildConfig,
    kas_yaml: Path,
    *,
    release: str = feed.DEFAULT_RELEASE,
    channel: str = feed.DEFAULT_CHANNEL,
    sboms: list[Path] | None = None,
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

    Preconditions: the caller has already run :func:`preflight_results` and
    refused on a blocking result. This does not re-check, because the useful
    place to fail is before a multi-hour build rather than after it.
    """
    result = feed.sync(
        cfg,
        deploy_dir=deploy_dir(cfg),
        scripts=feed.meta_avocado_scripts(kas_yaml),
        release=release,
        channel=channel,
        sboms=sboms,
    )
    result["index"] = feed_index.write_targets_index(Path(str(result["channel_root"])))
    return result
