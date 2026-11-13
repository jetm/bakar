"""Host-side description of a finished build run, behind ``bakar feed describe``.

Runs on the build host and only reads: it resolves a run to its build output,
judges whether that output can still change, and lists the files a pull needs
with size and checksum. It creates and writes nothing under the build tree or
the run registry, and holds no lock beyond one momentary shared test of the
build directory's lock file. The one thing it runs besides that test is read-only
``git rev-parse HEAD`` on each build layer directory; it never runs kas or
bitbake, so a recipe several repositories provide is recorded by its first layer
with a warning rather than resolved by asking bitbake.

It describes only the newest run of a build directory: every dispatch from one
checkout reuses that directory, so an older run's output may have been replaced.

The recipe entries come from :func:`bakar.commands._feed_refresh.build_entries`,
the code a local refresh uses, against the host's own build data, so the cache
key a pulled delta carries equals the one a local refresh of the same tree writes.
"""

from __future__ import annotations

import contextlib
import dataclasses
import fcntl
import hashlib
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import typer

from bakar import (
    detach_records,
    feed,
    feed_consolidate,
    feed_describe,
    feed_filter,
    layers,
    observability,
)
from bakar.commands import _feed_refresh as refresh
from bakar.gitutil import run_git
from bakar.release_workspace import REPO_POLICY_ENV

if TYPE_CHECKING:
    from collections.abc import Callable

    from bakar import feed_delta
    from bakar.config import BuildConfig

CHUNK_SIZE = 1024 * 1024
VENDOR_FAMILIES = ("nxp", "ti")
_ENV_KEYS = ("BAKAR_DISPATCH_KAS", "BAKAR_DISPATCH_WORKSPACE")
BUILD_DIR_ENV = "BAKAR_WORKTREE_BUILD_DIR"
_FULL_COMMIT = re.compile(r"[0-9a-f]{40}")
_RUN_ID = re.compile(r"(\d{8}-\d{6})-(\d+)")

LockState = Literal["held", "free", "unknown"]
LockProbe = tuple[LockState, str | None]


class DescribeError(RuntimeError):
    """The run cannot be described; the message says why and what to do."""


def probe_lock(path: Path) -> LockProbe:
    """One momentary non-blocking shared ``flock`` test of ``path``, opened read-only.

    A missing file means no build ever took the lock, so nobody holds it. Any
    other failure to test is ``unknown`` with the cause, never ``free``.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    except FileNotFoundError:
        return "free", None
    except OSError as exc:
        return "unknown", f"cannot open {path}: {exc.strerror or exc}"
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return "held", None
        except OSError as exc:
            return "unknown", f"cannot test {path}: {exc.strerror or exc}"
        fcntl.flock(fd, fcntl.LOCK_UN)
        return "free", None
    finally:
        os.close(fd)


def resolve_host_cfg(workspace: Path, kas_yaml: Path, *, mode: str | None, build_dir: str | None) -> BuildConfig:
    """Resolve the build configuration the run used, from the values its environment recorded.

    This is the refresh command's own resolution (``_resolve_refresh_cfg``), so
    the recipe locator, layer reader and package-data reader see the tree the
    build used. Three inputs of that resolution are process state rather than
    arguments, so each is applied for the call and restored in a ``finally``:
    the global ``--host``/``--container`` choice (``mode``, from the run's
    ``build_config`` event), the dispatch's ``BAKAR_WORKTREE_BUILD_DIR`` (it
    decides ``bsp_root``, which the describing process does not have set), and
    ``BAKAR_REPO_POLICY``, which the workspace routing may set. Anything the
    resolution prints goes to stderr so stdout carries only the document.
    """
    import bakar.commands._app as state
    from bakar.commands.feed import _resolve_refresh_cfg  # deferred: feed.py imports this module

    saved_opts = state._GLOBAL_OPTIONS
    saved_env = {key: os.environ.get(key) for key in (BUILD_DIR_ENV, REPO_POLICY_ENV)}
    if mode in ("host", "container"):
        state._GLOBAL_OPTIONS = dataclasses.replace(saved_opts, host=mode == "host", container=mode == "container")
    if build_dir:
        os.environ[BUILD_DIR_ENV] = build_dir
    try:
        with contextlib.redirect_stdout(sys.stderr):
            return _resolve_refresh_cfg(workspace, kas_yaml)
    except typer.Exit as exc:
        raise DescribeError(
            f"could not resolve the build configuration for {kas_yaml} in workspace {workspace} "
            f"(exit {exc.exit_code}); the message above names the cause"
        ) from exc
    finally:
        state._GLOBAL_OPTIONS = saved_opts
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def no_bitbake_provider(_cfg: BuildConfig, _kas_yaml: Path, _recipes: list[str]) -> None:
    """Refuse to ask bitbake which layer built a recipe, so describing stays read-only.

    The local refresh's provider opens a run, clears stale locks and runs kas and
    bitbake in the build host's workspace; none of that may happen on a describe.
    ``build_entries`` turns this refusal into a warning and records the first layer.
    """
    raise RuntimeError("refresh --from does not run bitbake on the build host")


@dataclass(frozen=True)
class DescribeDeps:
    """Seams for tests; the defaults read the real run registry, lock and configuration."""

    run_dir_for_run: Callable[[str], Path | None] = detach_records.run_dir_for_run
    lock_probe: Callable[[Path], LockProbe] = probe_lock
    resolve_cfg: Callable[..., BuildConfig] = resolve_host_cfg
    resolve_provider: refresh.ResolveProvider = no_bitbake_provider


def describe_build(
    run_id: str, recipes: tuple[str, ...], *, release: str | None = None, deps: DescribeDeps | None = None
) -> feed_describe.BuildDescription:
    """Describe run ``run_id`` for ``recipes``; raise :class:`DescribeError` when it cannot be.

    A run that failed or is still running is not an error: the document carries
    that state, no files and no entries, so the caller can name the state.
    """
    deps = deps or DescribeDeps()
    run_dir = deps.run_dir_for_run(run_id)
    if run_dir is None:
        raise DescribeError(f"no run {run_id} is registered on this host")
    config = observability.last_build_config(run_dir) or {}
    family = config.get("family")
    if family in VENDOR_FAMILIES:
        raise DescribeError(
            f"run {run_id} is a {family} build; remote refresh covers avocado release-workspace and layer builds only"
        )
    state, exit_code = _run_state(run_dir)
    machine = config.get("machine")
    tmpdir, _sstate = observability.last_build_paths(run_dir)
    if state != "succeeded":
        return _document(
            run_id,
            state,
            exit_code,
            machine=machine if isinstance(machine, str) else "",
            tmpdir=tmpdir,
            live=state == "running",
            live_reason=f"run is {state}" if state == "running" else None,
        )
    _require_newest_run(run_dir, run_id)
    if tmpdir is None:
        raise DescribeError(f"run {run_id} recorded no build output location, so there is nothing to describe")
    recorded = _require_dispatch_env(run_dir, run_id)
    live, live_reason = _liveness(run_dir, deps)
    deploy_root = tmpdir / "deploy"
    files, pkgdata, rpms = _files(run_id, tmpdir, deploy_root, recipes)
    mode = config.get("mode")
    # A colon-joined kas argument names the primary file first, as the build itself reads it.
    kas_yaml = Path(recorded["BAKAR_DISPATCH_KAS"].split(":")[0])
    cfg = deps.resolve_cfg(
        Path(recorded["BAKAR_DISPATCH_WORKSPACE"]),
        kas_yaml,
        mode=mode if mode in ("host", "container") else None,
        build_dir=recorded.get(BUILD_DIR_ENV),
    )
    declared_release, channel = feed_consolidate.declared_release_channel(deploy_root)
    entries, warnings = _entries(
        cfg,
        kas_yaml,
        declared_release or release or feed.DEFAULT_RELEASE,
        recipes=recipes,
        pkgdata=pkgdata,
        rpms=rpms,
        tmpdir=tmpdir,
        deploy_rpm=deploy_root / "rpm",
        resolve_provider=deps.resolve_provider,
    )
    layer_commits, bitbake_commit = _layer_commits(cfg)
    return _document(
        run_id,
        state,
        exit_code,
        machine=machine if isinstance(machine, str) else "",
        tmpdir=tmpdir,
        live=live,
        live_reason=live_reason,
        files=files,
        release=declared_release or "",
        channel=channel or "",
        entries=tuple(entries),
        layer_commits=layer_commits,
        bitbake_commit=bitbake_commit,
        warnings=warnings,
    )


def _entries(
    cfg: BuildConfig,
    kas_yaml: Path,
    release: str,
    *,
    recipes: tuple[str, ...],
    pkgdata: dict[str, refresh.RecipePkgdata],
    rpms: dict[str, list[str]],
    tmpdir: Path,
    deploy_rpm: Path,
    resolve_provider: refresh.ResolveProvider,
) -> tuple[list[feed_delta.RecipeEntry], tuple[str, ...]]:
    """The entries a local refresh would write, computed by the code it uses."""
    locations = {r: refresh.find_recipes(cfg, r) for r in recipes}
    unknown = [r for r, found in locations.items() if not found]
    if unknown:
        raise DescribeError(
            f"recipe not found in the configured layers: {', '.join(unknown)} "
            f"(layers are read from {getattr(cfg, 'bblayers_conf', 'the build configuration')})"
        )
    try:
        machine = refresh.target_slug(cfg.machine, deploy_rpm)
    except UnicodeDecodeError as exc:
        raise DescribeError(
            f"{feed_filter.REPO_MAP_NAME} is not valid UTF-8 ({exc.reason} at byte {exc.start})"
        ) from exc
    try:
        return refresh.build_entries(
            cfg,
            kas_yaml,
            release,
            recipes=recipes,
            pkgdata=pkgdata,
            rpms=rpms,
            tmpdir=tmpdir,
            machine=machine,
            locations=locations,
            resolve_provider=resolve_provider,
        )
    except refresh.RefreshFailedError as exc:
        raise DescribeError(str(exc).removesuffix("; the previous delta is untouched")) from exc


def _repo_key(repo: Path) -> str:
    """The kas ``repos:`` key of a layer repository.

    A directory ``<ws>/<key>`` is named ``<key>``. A side checkout lives at
    ``<ws>/.bakar/pins/<key>/<commit12>``, so its key is its parent's name.
    """
    pins = repo.parent.parent
    if pins.name == "pins" and pins.parent.name == ".bakar":
        return repo.parent.name
    return repo.name


def _head_commit(path: Path) -> str | None:
    """The full HEAD commit of the checkout at ``path``, else None (read-only)."""
    out = run_git(["git", "-C", str(path), "rev-parse", "HEAD"])
    if out is None or out.returncode != 0:
        return None
    commit = out.stdout.strip()
    return commit if _FULL_COMMIT.fullmatch(commit) else None


def _layer_commits(cfg: BuildConfig) -> tuple[tuple[feed_describe.LayerCommit, ...], str | None]:
    """The commit each build layer repository, and the bitbake directory, holds now.

    The repositories are the ones the build's ``bblayers.conf`` names, found by
    the same reader the recipe locator uses, so a side checkout is reported by
    its own commit and a directory that is not a checkout by a null commit.
    """
    repos: dict[str, Path] = {}
    for _layer, _layer_dir, repo in refresh._bblayers_layers(cfg):
        repos.setdefault(_repo_key(repo), repo)
    commits = tuple(feed_describe.LayerCommit(repo=key, commit=_head_commit(repo)) for key, repo in repos.items())
    bitbake_dir = layers._find_bitbake_dir(cfg, list(repos.values()))
    return commits, None if bitbake_dir is None else _head_commit(bitbake_dir)


def _document(
    run_id: str,
    state: str,
    exit_code: int | None,
    *,
    machine: str,
    tmpdir: Path | None,
    live: bool,
    live_reason: str | None,
    files: tuple[feed_describe.DescribedFile, ...] = (),
    release: str = "",
    channel: str = "",
    entries: tuple[feed_delta.RecipeEntry, ...] = (),
    layer_commits: tuple[feed_describe.LayerCommit, ...] = (),
    bitbake_commit: str | None = None,
    warnings: tuple[str, ...] = (),
) -> feed_describe.BuildDescription:
    return feed_describe.BuildDescription(
        schema_version=feed_describe.SCHEMA_VERSION,
        run_id=run_id,
        state=state,
        exit_code=exit_code,
        machine=machine,
        release=release,
        channel=channel,
        tmpdir="" if tmpdir is None else str(tmpdir),
        deploy_dir="" if tmpdir is None else str(tmpdir / "deploy"),
        live=live,
        live_reason=live_reason,
        files=files,
        entries=entries,
        layers=layer_commits,
        bitbake_commit=bitbake_commit,
        warnings=warnings,
    )


def _run_state(run_dir: Path) -> tuple[str, int | None]:
    rec = observability.last_run_event(run_dir / "events.jsonl", lambda r: r.get("event") in ("run_end", "run_error"))
    if rec is None:
        return "running", None
    code = rec.get("exit_code")
    # bool is an int subclass; a boolean is not a recorded exit status.
    code = code if isinstance(code, int) and not isinstance(code, bool) else None
    return ("succeeded" if code == 0 else "failed"), code


def _run_key(name: str) -> tuple[str, int] | None:
    match = _RUN_ID.fullmatch(name)
    return None if match is None else (match.group(1), int(match.group(2)))


def _require_newest_run(run_dir: Path, run_id: str) -> None:
    """Refuse a run that a later run of the same build directory followed.

    Every dispatch from one checkout reuses one build directory, so the deploy
    tree, package data and layer checkouts read now may be a later run's.
    """
    own = _run_key(run_id)
    if own is None:
        return  # _files refuses an id with no timestamp before reading anything
    try:
        siblings = list(run_dir.parent.iterdir())
    except OSError as exc:
        raise DescribeError(f"cannot list the runs in {run_dir.parent}: {exc.strerror or exc}") from exc
    newer = [
        (key, p.name)
        for p in siblings
        if (key := _run_key(p.name)) is not None and key > own and not p.is_symlink() and p.is_dir()
    ]
    if newer:
        newest = max(newer)[1]
        raise DescribeError(
            f"run {run_id} is not the newest build in its build directory: run {newest} ran there after it, "
            f"so the output of {run_id} may have been replaced; refresh from the newest run ({newest})"
        )


def _require_dispatch_env(run_dir: Path, run_id: str) -> dict[str, str]:
    """The run's recorded environment; refuse one that lacks the two dispatch variables."""
    recorded: dict[str, str] = {}
    try:
        for line in (run_dir / "env.txt").read_text(errors="replace").splitlines():
            key, sep, value = line.partition("=")
            if sep and value:
                recorded[key] = value
    except OSError:
        pass
    missing = [k for k in _ENV_KEYS if k not in recorded]
    if missing:
        raise DescribeError(
            f"run {run_id} predates remote refresh support (its environment lacks {', '.join(missing)}); "
            "the build must be dispatched again"
        )
    return recorded


def _liveness(run_dir: Path, deps: DescribeDeps) -> tuple[bool, str | None]:
    lock = run_dir.parent.parent / "bitbake.lock"
    state, detail = deps.lock_probe(lock)
    if state == "held":
        return True, f"a build holds {lock}"
    if state == "unknown":
        return True, f"lock state unknown: {detail}" if detail else f"lock state unknown for {lock}"
    return False, None


def _before_run(rpm_dir: Path, run_start: float) -> dict[str, int]:
    """``<arch>/<file>`` to mtime for package files last written before the run started."""
    start_ns = int(run_start * 1_000_000_000)
    return {key: mtime for key, mtime in refresh._rpm_snapshot(rpm_dir).items() if mtime < start_ns}


def _files(
    run_id: str, tmpdir: Path, deploy_root: Path, recipes: tuple[str, ...]
) -> tuple[tuple[feed_describe.DescribedFile, ...], dict[str, refresh.RecipePkgdata], dict[str, list[str]]]:
    """The files to pull, with the package data and package listing they were derived from."""
    rpm_dir = deploy_root / "rpm"
    repo_map = rpm_dir / feed_filter.REPO_MAP_NAME
    if not repo_map.is_file():
        raise DescribeError(f"{repo_map} does not exist, so the build has no avocado package output to pull")
    run_start = observability.run_id_epoch(run_id)
    if run_start is None:
        raise DescribeError(f"run id {run_id} does not start with a UTC timestamp, so its start time is unknown")
    try:
        pkgdata = {r: refresh.read_recipe_pkgdata(tmpdir, r) for r in recipes}
        own_names = frozenset(n for pd in pkgdata.values() for n in pd.packages)
        stale = refresh.stale_map_message(rpm_dir, own_names, _before_run(rpm_dir, run_start))
        rpms = refresh.rpm_files(rpm_dir)
    except OSError as exc:
        raise DescribeError(f"could not read the build output under {deploy_root}: {exc}") from exc
    if stale:
        raise DescribeError(stale)
    missing = [r for r, pd in pkgdata.items() if not any(n in rpms for n in pd.packages)]
    if missing:
        raise DescribeError(f"the build output holds no packages for {', '.join(missing)}")
    paths = {repo_map}
    for pd in pkgdata.values():
        for name in pd.packages:
            for filename in rpms.get(name, ()):
                paths.update(arch / filename for arch in sorted(rpm_dir.iterdir()) if (arch / filename).is_file())
    # The pull never follows links, and OE's unversioned image-link name is a link to the versioned file.
    paths.update(p for p in deploy_root.glob("images/*/*.testdata.json") if not p.is_symlink())
    return tuple(_describe_file(deploy_root, p) for p in sorted(paths)), pkgdata, rpms


def _describe_file(deploy_root: Path, path: Path) -> feed_describe.DescribedFile:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as fh:
            while chunk := fh.read(CHUNK_SIZE):
                digest.update(chunk)
                size += len(chunk)
    except OSError as exc:
        raise DescribeError(f"cannot read {path}: {exc.strerror or exc}") from exc
    return feed_describe.DescribedFile(
        path=path.relative_to(deploy_root).as_posix(), size=size, sha256=digest.hexdigest()
    )
