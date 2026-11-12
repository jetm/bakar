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
import re
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


@dataclass(frozen=True, slots=True)
class RecipeLocation:
    """A recipe file and the layer repository that holds it."""

    path: Path
    layer: str
    repo_path: Path


def _is_pruned(name: str) -> bool:
    """Directories a recipe scan skips: git metadata and bitbake ``tmp`` trees."""
    return name == ".git" or name == "tmp" or name.startswith(("tmp-", "tmp_"))


def _layer_repos(cfg: BuildConfig) -> list[tuple[str, Path]]:
    """Return ``(name, resolved_path)`` for each layer repository the workspace holds.

    Follows the ``layers/`` (bitbake-setup) and ``sources/`` (kas) conventions
    that :func:`bakar.layers.collect_layer_hashes` reads. They are looked up
    under the workspace and under ``bsp_root``, which differ for meta-avocado
    (``bsp_root`` is a ``build-<stem>`` directory inside the workspace). Symlinked
    layers are followed and de-duplicated by their real path. A BYO build with
    neither directory scans ``bsp_root`` itself.
    """
    seen: set[Path] = set()
    found: list[tuple[str, Path]] = []
    for base in dict.fromkeys((cfg.workspace, cfg.bsp_root)):
        for dirname in _LAYER_DIRS:
            root = base / dirname
            if not root.is_dir():
                continue
            try:
                entries = sorted(root.iterdir())
            except OSError:
                continue
            for entry in entries:
                real = entry.resolve()
                if entry.is_dir() and real not in seen:
                    seen.add(real)
                    found.append((entry.name, real))
    return found or [(cfg.bsp_root.name, cfg.bsp_root.resolve())]


def find_recipe(cfg: BuildConfig, recipe: str) -> RecipeLocation | None:
    """Locate ``<recipe>.bb`` or ``<recipe>_<version>.bb`` in the workspace's layer repositories."""
    exact, prefix = f"{recipe}.bb", f"{recipe}_"
    for layer, repo in _layer_repos(cfg):
        visited: set[str] = set()
        for dirpath, dirnames, filenames in os.walk(repo, followlinks=True):
            real = os.path.realpath(dirpath)
            if real in visited:
                dirnames[:] = []
                continue
            visited.add(real)
            dirnames[:] = sorted(d for d in dirnames if not _is_pruned(d))
            for name in sorted(filenames):
                if name == exact or (name.startswith(prefix) and name.endswith(".bb")):
                    return RecipeLocation(path=Path(dirpath) / name, layer=layer, repo_path=repo)
    return None


def layer_has_recipe(cfg: BuildConfig, recipe: str) -> bool:
    """True when :func:`find_recipe` finds the recipe in the workspace layers."""
    return find_recipe(cfg, recipe) is not None


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


def read_recipe_pkgdata(tmpdir: Path, recipe: str) -> RecipePkgdata:
    """Read a built recipe's RPM package names and epoch from ``pkgdata``.

    ``pkgdata/<pkgarch>/<recipe>`` lists ``PACKAGES``; a package renamed at
    packaging time (``PKG:<pkg>``) is published under the new name, so the
    runtime file decides the RPM name. Without pkgdata the recipe's own name is
    the only name that can be assumed. The recipe file carries no layer or
    directory information, so neither is read here.
    """
    recipe_files = sorted(p for p in (tmpdir / "pkgdata").glob(f"*/{recipe}") if p.is_file())
    if not recipe_files:
        return RecipePkgdata(packages=[recipe], epoch=0)
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
    return RecipePkgdata(packages=names, epoch=epoch)


def _layer_commit(loc: RecipeLocation, hashes: list[layers.LayerHash]) -> str:
    """The abbreviated commit of the layer repository holding the recipe.

    Prefers the bblayers-derived hash for the repository by name; a layer that
    list does not carry (meta-avocado, whose ``bsp_root`` is a build directory)
    is asked directly.
    """
    for h in hashes:
        if h.repo == loc.layer:
            return h.short_hash
    return layers._git_short_hash(loc.repo_path) or ""


def _srcrev_of(pv: str) -> str | None:
    """The abbreviated source revision of a ``+git`` version, else None.

    A recipe following a git branch has ``PKGV`` of the form
    ``3.5.0+git0+0123abcd``: the hash is the text after the last ``+``. A release
    version carries none, and nothing else in the deploy tree records one.
    """
    if "+git" not in pv:
        return None
    tail = pv.rsplit("+", 1)[1]
    return tail if re.fullmatch(r"[0-9a-f]{7,40}", tail) else None


def _version_key(text: str) -> tuple[tuple[int, int, str], ...]:
    """Order key for a version or release string, comparing digit runs numerically.

    Digit runs compare as integers and sort above letter runs; a longer string
    wins when it extends a shorter one. This approximates ``rpmvercmp`` for
    bitbake's ``<n>.<m>`` releases and dotted versions; it does not handle the
    ``~`` and ``^`` operators or epochs.
    """
    return tuple((1, int(tok), "") if tok.isdigit() else (0, 0, tok) for tok in re.findall(r"\d+|[A-Za-z]+", text))


def _rpm_files(deploy: Path) -> dict[str, list[str]]:
    """Map RPM package name to the file names of its newest build in the deploy tree.

    A PR bump leaves ``foo-1.0-r0`` and ``foo-1.0-r1`` side by side; only the
    newest per (name, arch) is kept, so one recipe never publishes two builds.
    """
    best: dict[tuple[str, str], tuple[tuple[object, object], str]] = {}
    for rpm in sorted(deploy.glob("*/*.rpm")):
        try:
            nevra = feed_delta.parse_nevra(rpm.name)
        except ValueError:
            continue
        key = (_version_key(nevra.version), _version_key(nevra.release))
        slot = (nevra.name, nevra.arch)
        if slot not in best or key > best[slot][0]:
            best[slot] = (key, rpm.name)
    found: dict[str, list[str]] = {}
    for (name, _arch), (_key, filename) in sorted(best.items()):
        found.setdefault(name, []).append(filename)
    return found


def _rpm_snapshot(deploy: Path) -> dict[str, int]:
    """Map ``<arch>/<file>`` to mtime in nanoseconds for every RPM in the deploy tree."""
    out: dict[str, int] = {}
    for rpm in deploy.glob("*/*.rpm"):
        try:
            out[f"{rpm.parent.name}/{rpm.name}"] = rpm.stat().st_mtime_ns
        except OSError:
            continue
    return out


def _stale_map_message(deploy: Path, own_names: frozenset[str], before: dict[str, int]) -> str | None:
    """Refuse reason when ``avocado-repo.map`` predates packages that were there before the refresh.

    Spike 1.2 measured that a leaf-recipe build does not rewrite the map, so the
    map is always older than the refresh's own packages and comparing against
    them would refuse every refresh. For the same reason a runtime dependency
    this build rewrote is newer than the map without the map having been stale.
    The comparison therefore uses only RPMs that were already in the deploy tree
    before the build (``before``, from :func:`_rpm_snapshot`) and were not
    rewritten by it, excluding the recipes' own packages: a map older than those
    was stale before this refresh started.
    """
    repo_map = deploy / feed_filter.REPO_MAP_NAME
    if not repo_map.is_file():
        return None
    mtimes: list[int] = []
    for key, mtime in _rpm_snapshot(deploy).items():
        if before.get(key) != mtime:
            continue
        try:
            if feed_delta.parse_nevra(key.rpartition("/")[2]).name not in own_names:
                mtimes.append(mtime)
        except ValueError:
            continue
    if mtimes and repo_map.stat().st_mtime_ns < max(mtimes):
        return (
            f"{repo_map} is older than packages already in {deploy}, so a sync from it can omit them; "
            "rebuild an image or the avocado-complete target to rewrite the map, then retry"
        )
    return None


def _wipe(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def _sibling(path: Path, suffix: str) -> Path:
    return path.with_name(path.name + suffix)


def _swap_in(new: Path, final: Path) -> Path | None:
    """Move ``new`` to ``final``, setting any existing ``final`` aside. Returns the aside path."""
    final.parent.mkdir(parents=True, exist_ok=True)
    aside = None
    if final.exists():
        aside = _sibling(final, ".old")
        _wipe(aside)
        final.rename(aside)
    try:
        new.rename(final)
    except OSError:
        if aside is not None:
            aside.rename(final)
        raise
    return aside


def _swap_all(pairs: list[tuple[Path, Path]]) -> None:
    """Swap every ``(new, final)`` pair into place, or restore every old directory and re-raise."""
    done: list[tuple[Path, Path | None]] = []
    try:
        for new, final in pairs:
            done.append((final, _swap_in(new, final)))
    except BaseException:
        for final, aside in reversed(done):
            _wipe(final)
            if aside is not None:
                aside.rename(final)
        raise
    for _final, aside in done:
        if aside is not None:
            _wipe(aside)


def _build_entries(
    req: RefreshRequest,
    *,
    recipes: tuple[str, ...],
    pkgdata: dict[str, RecipePkgdata],
    rpms: dict[str, list[str]],
    tmpdir: Path,
    locate: Callable[[BuildConfig, str], RecipeLocation | None],
) -> list[feed_delta.RecipeEntry]:
    """Build and validate every recipe entry. Raises before anything is published."""
    hashes = layers.collect_layer_hashes(req.cfg)
    entries = []
    for recipe in recipes:
        pd = pkgdata[recipe]
        names = [n for n in pd.packages if n in rpms]
        files = [f for n in names for f in rpms[n]]
        loc = locate(req.cfg, recipe)
        layer, commit = (loc.layer, _layer_commit(loc, hashes)) if loc else ("", "")
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
            raise RefreshFailedError(f"{exc}; the previous delta is untouched") from exc
        task_hash = feed_delta.read_task_hash(tmpdir / "stamps", recipe, probe.pv)
        entries.append(dataclasses.replace(probe, task_hash=task_hash, srcrev=_srcrev_of(probe.pv)))
    return entries


def run_refresh(
    req: RefreshRequest,
    *,
    builder: Callable[..., BuildResult] | None = None,
    recipe_exists: Callable[[BuildConfig, str], bool] = layer_has_recipe,
    locate_recipe: Callable[[BuildConfig, str], RecipeLocation | None] = find_recipe,
) -> RefreshResult:
    """Build the named recipes and replace the delta with exactly their packages.

    Order is the contract. The guards and the unknown-recipe check run before the
    build; the map-staleness check, the empty-output check and every manifest
    entry (including the version-release agreement of a recipe's files) run after
    it, and only then is anything published. The delta and its stage are written
    into fresh ``.new`` siblings, ``delta.json`` and ``recipe-map.json`` included,
    and swapped into place last, so a refused or failed refresh, at any step,
    leaves the previous delta in place.

    The workspace is ``cfg.workspace`` for the build and the live-build guard
    alike; ``cfg.bsp_root`` is a build directory inside it for meta-avocado and
    is not a workspace. The tmpdir, deploy and stamps directories are the cfg's
    own, which is where a build launched against that workspace writes.
    """
    recipes = tuple(dict.fromkeys(req.recipes))
    if not recipes:
        raise RefreshFailedError("name at least one recipe to build; nothing was built")
    build = builder or bitbake_cmd.build_targets
    workspace = req.cfg.workspace
    with refresh_lock(req.feed_root):
        for root in (req.delta_root, req.work_root):
            check_guards(
                workspace=workspace,
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

        tmpdir = req.cfg.resolved_tmpdir
        deploy = feed_ops.deploy_dir(req.cfg)
        before = _rpm_snapshot(deploy)
        result = build(list(recipes), kas_yaml=str(req.kas_yaml), workspace=workspace)
        if result.exit_code != 0:
            raise RefreshFailedError(
                f"build failed (exit {result.exit_code}, run {result.run_id}); the previous delta is untouched"
            )

        pkgdata = {r: read_recipe_pkgdata(tmpdir, r) for r in recipes}
        own_names = frozenset(n for pd in pkgdata.values() for n in pd.packages)
        stale = _stale_map_message(deploy, own_names, before)
        if stale:
            raise RefreshFailedError(f"{stale}; the previous delta is untouched")
        rpms = _rpm_files(deploy)
        missing = [r for r, pd in pkgdata.items() if not any(n in rpms for n in pd.packages)]
        if missing:
            raise RefreshFailedError(
                f"the build produced no packages for {', '.join(missing)}; the previous delta is untouched"
            )
        entries = _build_entries(req, recipes=recipes, pkgdata=pkgdata, rpms=rpms, tmpdir=tmpdir, locate=locate_recipe)

        channel_dir = feed.channel_root(req.delta_root, release=req.release, channel=req.channel)
        stage_dir = feed.channel_root(req.work_root, release=req.release, channel=req.channel)
        new_delta, new_stage = _sibling(req.delta_root, ".new"), _sibling(req.work_root, ".new")
        kept = {f for n in own_names for f in rpms.get(n, [])}
        view = Path(tempfile.mkdtemp(prefix="feed-view-", dir=deploy.parent))
        for fresh in (new_delta, new_stage):
            _wipe(fresh)
        try:
            try:
                view_dir = feed_filter.build_filtered_view(deploy, view, own_names)
            except FileNotFoundError as exc:
                raise RefreshFailedError(f"{exc}; the previous delta is untouched") from exc
            for linked in view_dir.glob("*/*.rpm"):
                if linked.name not in kept:
                    linked.unlink()
            sync = feed.sync(
                req.cfg,
                publish=feed.FeedPublish(
                    deploy_dir=view_dir,
                    scripts=scripts,
                    release=req.release,
                    channel=req.channel,
                    head_only=not req.snapshot,
                ),
                feed_root=new_delta,
                stage_root=new_stage,
            )
            new_channel = feed.channel_root(new_delta, release=req.release, channel=req.channel)
            new_channel.mkdir(parents=True, exist_ok=True)
            feed_delta.write_recipe_map(new_channel, entries)
            feed_delta.write_delta(
                new_channel,
                base_release=req.base_release,
                base_channel=req.base_channel,
                built_recipes=list(recipes),
                bakar_run_id=result.run_id,
                delta_path=str(channel_dir),
                snapshot_id=req.base_snapshot_id,
            )
            new_stage_channel = feed.channel_root(new_stage, release=req.release, channel=req.channel)
            new_stage_channel.mkdir(parents=True, exist_ok=True)
            _swap_all([(new_channel, channel_dir), (new_stage_channel, stage_dir)])
        finally:
            for scratch in (view, new_delta, new_stage):
                _wipe(scratch)
    return RefreshResult(channel_dir=channel_dir, run_id=result.run_id, recipes=recipes, sync=sync, entries=entries)
