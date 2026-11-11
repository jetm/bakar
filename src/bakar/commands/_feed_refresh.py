"""Guards and pipeline for ``bakar feed refresh``.

A refresh wipes and rewrites the delta channel directory, so it must not run
beside another refresh, beside a build that is writing the same workspace, over a
mirrored repository, or with a stage root that is really the synced feed. Every
refusal here raises before the caller writes anything; the lock file is the only
file created ahead of them.

:func:`run_refresh` is the pipeline behind the guards: build the named recipes,
publish only their packages head-only into the delta root, and record what was
built in ``delta.json`` and ``recipe-map.json``.
"""

from __future__ import annotations

import dataclasses
import fcntl
import os
import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from bakar import feed, feed_delta, feed_filter, feed_ops, feed_preflight, layers, run_discovery
from bakar.commands import bitbake as bitbake_cmd

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from bakar.commands.bitbake import BuildResult
    from bakar.config import BuildConfig
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


class RefreshFailedError(RuntimeError):
    """The refresh could not complete; the message says what was missing and what was left untouched."""


@dataclass(frozen=True, kw_only=True, slots=True)
class RefreshRequest:
    """One refresh: what to build, where the delta lands, and what it was built against.

    ``delta_root`` is the feed root the delta is published into and ``work_root``
    is the stage root the publish unions into; both are wiped (their
    ``release/channel`` directory only) before the new delta is written.
    ``feed_root`` and ``sync_stage_root`` are the ordinary sync's roots, named so
    the guards can refuse a delta root that aliases them. ``base_*`` describe the
    published feed the delta was built against and come from the caller.
    """

    cfg: BuildConfig
    kas_yaml: Path
    recipes: tuple[str, ...]
    feed_root: Path
    sync_stage_root: Path
    delta_root: Path
    work_root: Path
    release: str = feed.DEFAULT_RELEASE
    channel: str = feed.DEFAULT_CHANNEL
    base_release: str = feed.DEFAULT_RELEASE
    base_channel: str = feed.DEFAULT_CHANNEL
    base_snapshot_id: str | None = None
    snapshot: bool = False
    user_config: UserConfig | None = None


@dataclass(frozen=True, slots=True)
class RefreshResult:
    channel_dir: Path
    run_id: str
    recipes: tuple[str, ...]
    sync: dict[str, object]
    entries: list[feed_delta.RecipeEntry]


_LAYER_DIRS = ("layers", "sources")


def layer_has_recipe(cfg: BuildConfig, recipe: str) -> bool:
    """True when a ``<recipe>.bb`` or ``<recipe>_<version>.bb`` exists in the workspace layers.

    Searches the ``layers/`` and ``sources/`` layer conventions under the BSP
    root, and the BSP root itself when neither exists. ``.git`` is pruned.
    """
    roots = [cfg.bsp_root / d for d in _LAYER_DIRS if (cfg.bsp_root / d).is_dir()] or [cfg.bsp_root]
    exact, prefix = f"{recipe}.bb", f"{recipe}_"
    for root in roots:
        for _dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d != ".git"]
            if any(f == exact or (f.startswith(prefix) and f.endswith(".bb")) for f in filenames):
                return True
    return False


def _read_pkgdata(path: Path) -> dict[str, str]:
    """Parse a bitbake pkgdata file of ``KEY: value`` lines; later keys win."""
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        key, sep, value = line.partition(": ")
        if sep:
            out[key] = value
    return out


@dataclass(frozen=True, slots=True)
class RecipePkgdata:
    packages: list[str]
    epoch: int
    file_dirname: str | None


def read_recipe_pkgdata(tmpdir: Path, recipe: str) -> RecipePkgdata:
    """Read a built recipe's RPM package names, epoch and recipe directory from ``pkgdata``.

    ``pkgdata/<pkgarch>/<recipe>`` lists ``PACKAGES``; a package renamed at
    packaging time (``PKG:<pkg>``) is published under the new name, so the
    runtime file decides the RPM name. Without pkgdata the recipe's own name is
    the only name that can be assumed.
    """
    recipe_files = sorted(p for p in (tmpdir / "pkgdata").glob(f"*/{recipe}") if p.is_file())
    if not recipe_files:
        return RecipePkgdata(packages=[recipe], epoch=0, file_dirname=None)
    data = _read_pkgdata(recipe_files[0])
    runtime = recipe_files[0].parent / "runtime"
    names: list[str] = []
    epoch = 0
    for pkg in data.get("PACKAGES", "").split() or [recipe]:
        rt = runtime / pkg
        rpm_name = pkg
        if rt.is_file():
            rt_data = _read_pkgdata(rt)
            rpm_name = rt_data.get(f"PKG:{pkg}", pkg).strip() or pkg
            if pkg == recipe and rt_data.get("PKGE", "").strip().isdigit():
                epoch = int(rt_data["PKGE"])
        if rpm_name not in names:
            names.append(rpm_name)
    return RecipePkgdata(packages=names, epoch=epoch, file_dirname=data.get("FILE_DIRNAME"))


def _layer_of(file_dirname: str | None, hashes: list[layers.LayerHash]) -> tuple[str, str]:
    """Return ``(layer, layer_commit)``: the first path component naming a known layer repository."""
    if file_dirname:
        by_repo = {h.repo: h.short_hash for h in hashes}
        for part in Path(file_dirname).parts:
            if part in by_repo:
                return part, by_repo[part]
    return "", ""


def _rpm_files(deploy: Path) -> dict[str, list[str]]:
    """Map RPM package name to the file names in the deploy tree."""
    found: dict[str, list[str]] = {}
    for rpm in sorted(deploy.glob("*/*.rpm")):
        try:
            found.setdefault(feed_delta.parse_nevra(rpm.name).name, []).append(rpm.name)
        except ValueError:
            continue
    return found


def _stale_map_message(deploy: Path, own_names: frozenset[str]) -> str | None:
    """Refuse reason when ``avocado-repo.map`` predates packages this refresh did not build.

    Spike 1.2 measured that a leaf-recipe build does not rewrite the map, so the
    map is always older than the refresh's own packages and comparing against
    them would refuse every refresh. The comparison therefore uses only the
    packages already in the deploy tree before the build: a map older than those
    was stale before this refresh started.
    """
    repo_map = deploy / feed_filter.REPO_MAP_NAME
    if not repo_map.is_file():
        return None
    mtimes: list[float] = []
    for rpm in deploy.glob("*/*.rpm"):
        try:
            if feed_delta.parse_nevra(rpm.name).name not in own_names:
                mtimes.append(rpm.stat().st_mtime)
        except ValueError:
            continue
    if mtimes and repo_map.stat().st_mtime < max(mtimes):
        return (
            f"{repo_map} is older than packages already in {deploy}, so a sync from it can omit them; "
            "rebuild an image or the avocado-complete target to rewrite the map, then retry"
        )
    return None


def _wipe(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def run_refresh(
    req: RefreshRequest,
    *,
    builder: Callable[..., BuildResult] | None = None,
    recipe_exists: Callable[[BuildConfig, str], bool] = layer_has_recipe,
) -> RefreshResult:
    """Build the named recipes and replace the delta with exactly their packages.

    Order is the contract. The guards and the unknown-recipe check run before the
    build, the map-staleness and empty-output checks run after it, and the delta
    is wiped only once everything that can refuse has: a refused or failed
    refresh leaves the previous delta in place.
    """
    recipes = tuple(dict.fromkeys(req.recipes))
    if not recipes:
        raise RefreshFailedError("name at least one recipe to build; nothing was built")
    build = builder or bitbake_cmd.build_targets
    with refresh_lock(req.feed_root):
        for root in (req.delta_root, req.work_root):
            check_guards(
                workspace=req.cfg.bsp_root,
                feed_root=req.feed_root,
                sync_stage_root=req.sync_stage_root,
                delta_stage_root=root,
                release=req.release,
                channel=req.channel,
                user_config=req.user_config,
            )
        scripts = feed_ops.scripts_dir(req.kas_yaml)
        if scripts is None:
            raise RefreshFailedError(f"meta-avocado scripts not found for {req.kas_yaml}; nothing was built")
        unknown = [r for r in recipes if not recipe_exists(req.cfg, r)]
        if unknown:
            raise RefreshFailedError(
                f"recipe not found in the configured layers: {', '.join(unknown)}; nothing was built"
            )

        result = build(list(recipes), kas_yaml=str(req.kas_yaml), workspace=req.cfg.bsp_root)
        if result.exit_code != 0:
            raise RefreshFailedError(
                f"build failed (exit {result.exit_code}, run {result.run_id}); the previous delta is untouched"
            )

        tmpdir = req.cfg.resolved_tmpdir
        deploy = feed_ops.deploy_dir(req.cfg)
        pkgdata = {r: read_recipe_pkgdata(tmpdir, r) for r in recipes}
        own_names = frozenset(n for pd in pkgdata.values() for n in pd.packages)
        stale = _stale_map_message(deploy, own_names)
        if stale:
            raise RefreshFailedError(f"{stale}; the previous delta is untouched")
        rpms = _rpm_files(deploy)
        missing = [r for r, pd in pkgdata.items() if not any(n in rpms for n in pd.packages)]
        if missing:
            raise RefreshFailedError(
                f"the build produced no packages for {', '.join(missing)}; the previous delta is untouched"
            )

        channel_dir = feed.channel_root(req.delta_root, release=req.release, channel=req.channel)
        view = Path(tempfile.mkdtemp(prefix="feed-view-", dir=deploy.parent))
        try:
            try:
                view_dir = feed_filter.build_filtered_view(deploy, view, own_names)
            except FileNotFoundError as exc:
                raise RefreshFailedError(f"{exc}; the previous delta is untouched") from exc
            _wipe(channel_dir)
            _wipe(feed.channel_root(req.work_root, release=req.release, channel=req.channel))
            sync = feed.sync(
                req.cfg,
                publish=feed.FeedPublish(
                    deploy_dir=view_dir,
                    scripts=scripts,
                    release=req.release,
                    channel=req.channel,
                    head_only=not req.snapshot,
                ),
                feed_root=req.delta_root,
                stage_root=req.work_root,
            )
        finally:
            _wipe(view)

        hashes = layers.collect_layer_hashes(req.cfg)
        entries = []
        for recipe in recipes:
            pd = pkgdata[recipe]
            names = [n for n in pd.packages if n in rpms]
            files = [f for n in names for f in rpms[n]]
            layer, commit = _layer_of(pd.file_dirname, hashes)
            try:
                probe = feed_delta.entry_from_package_files(
                    recipe=recipe,
                    package_files=files,
                    packages=names,
                    layer=layer,
                    epoch=pd.epoch,
                    srcrev=None,
                    layer_commit=commit,
                    machine=req.cfg.machine,
                    release=req.release,
                    task_hash=None,
                )
            except ValueError as exc:
                raise RefreshFailedError(str(exc)) from exc
            task_hash = feed_delta.read_task_hash(tmpdir / "stamps", recipe, probe.pv)
            entries.append(dataclasses.replace(probe, task_hash=task_hash))
        channel_dir.mkdir(parents=True, exist_ok=True)
        feed_delta.write_recipe_map(channel_dir, entries)
        feed_delta.write_delta(
            channel_dir,
            base_release=req.base_release,
            base_channel=req.base_channel,
            built_recipes=list(recipes),
            bakar_run_id=result.run_id,
            delta_path=str(channel_dir),
            snapshot_id=req.base_snapshot_id,
        )
    return RefreshResult(channel_dir=channel_dir, run_id=result.run_id, recipes=recipes, sync=sync, entries=entries)
