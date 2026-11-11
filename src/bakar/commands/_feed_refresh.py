"""Guards for ``bakar feed refresh``: nothing touches feed content until they pass.

A refresh wipes and rewrites the delta channel directory, so it must not run
beside another refresh, beside a build that is writing the same workspace, over a
mirrored repository, or with a stage root that is really the synced feed. Every
refusal here raises before the caller writes anything; the lock file is the only
file created ahead of them.
"""

from __future__ import annotations

import fcntl
from contextlib import contextmanager
from typing import TYPE_CHECKING

from bakar import feed, feed_preflight, run_discovery

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from bakar.diagnostics import CheckResult
    from bakar.user_config import UserConfig

LOCK_NAME = ".refresh.lock"


class RefreshRefusedError(RuntimeError):
    """A refresh guard refused; the message names the cause and the way out."""


@contextmanager
def refresh_lock(feed_dir: Path) -> Iterator[None]:
    """Hold an exclusive non-blocking ``flock`` on ``<feed_dir>/.refresh.lock``.

    The lock cannot exist before its file does, so the file is opened (created
    when absent) and locked in one step, and nothing else is written first. A
    second holder is refused rather than queued: the second refresh would wipe
    the delta the first is still building.
    """
    feed_dir.mkdir(parents=True, exist_ok=True)
    lock_path = feed_dir / LOCK_NAME
    with lock_path.open("a+", encoding="utf-8") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RefreshRefusedError(f"another refresh holds {lock_path}; wait for it to finish, then retry") from exc
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def check_stage_root(delta_stage_root: Path, *, feed_root: Path, sync_stage_root: Path) -> None:
    """Refuse a delta stage root that resolves to what ``feed sync`` publishes or stages.

    The refresh wipes its stage root. Resolved, so a symlink onto the synced
    feed does not slip past a string comparison.
    """
    resolved = delta_stage_root.resolve()
    if resolved == feed_root.resolve():
        raise RefreshRefusedError(
            f"the delta stage root {delta_stage_root} resolves to the synced feed root {feed_root}; "
            "a refresh would wipe the published packages"
        )
    if resolved == sync_stage_root.resolve():
        raise RefreshRefusedError(
            f"the delta stage root {delta_stage_root} resolves to the synced stage root {sync_stage_root}; "
            "a refresh would wipe the packages `feed sync` accumulated"
        )


def check_no_mirror_markers(channel_dir: Path) -> None:
    """Refuse when any sub-repository under the delta channel directory is a mirror."""
    if not channel_dir.is_dir():
        return
    marked = sorted(p.parent for p in channel_dir.rglob(feed.MIRROR_MARKER))
    if marked:
        raise RefreshRefusedError(
            f"{marked[0]} carries {feed.MIRROR_MARKER}, so `bakar feed mirror` created it; "
            "a refresh would replace another publisher's metadata. Choose another feed root "
            "or remove the mirrored repository"
        )


def check_no_live_build(workspace: Path, *, user_config: UserConfig | None = None) -> None:
    """Refuse while a build is running in the workspace.

    A running build rewrites the deploy tree the refresh stages from.
    """
    live = run_discovery.live_workspace_runs(workspace, user_config=user_config)
    if live:
        names = ", ".join(c.run_dir.name for c in live)
        raise RefreshRefusedError(
            f"a build is running in {workspace} ({names}); wait for it or `bakar stop` it, then retry"
        )


def check_guards(
    *,
    workspace: Path,
    feed_root: Path,
    sync_stage_root: Path,
    delta_stage_root: Path,
    release: str,
    channel: str,
    user_config: UserConfig | None = None,
) -> None:
    """Run every refusal check, cheapest and least stateful first. Writes nothing."""
    check_stage_root(delta_stage_root, feed_root=feed_root, sync_stage_root=sync_stage_root)
    check_no_live_build(workspace, user_config=user_config)
    check_no_mirror_markers(feed.channel_root(delta_stage_root, release=release, channel=channel))


def preflight_results(
    *,
    scripts: Path | None,
    deploy_dir: Path | None,
    roots: feed.FeedRoots | None,
    release: str,
    channel: str,
) -> list[CheckResult]:
    """Preflight that works before a first build.

    ``deploy_dir=None`` skips the build-output checks, since the refresh builds
    first and a deploy tree does not exist yet. Pass the deploy directory after
    the build to run ``check_build_output`` and the freshness checks. A missing
    scripts checkout is reported in both phases.
    """
    results = feed_preflight.preflight(
        roots=roots,
        build=feed_preflight.PreflightBuild(deploy_dir=deploy_dir, scripts=scripts, release=release, channel=channel),
    )
    if scripts is None and deploy_dir is None:
        results.extend(feed_preflight.check_scripts(None))
    return results
