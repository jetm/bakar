"""Host-side description of a finished build run, behind ``bakar feed describe``.

Runs on the build host and only reads: it resolves a run to its build output,
judges whether that output can still change, and lists the files a pull needs
with size and checksum. It creates and writes nothing under the build tree or
the run registry, and holds no lock beyond one momentary shared test of the
build directory's lock file. ``entries`` and ``layers`` are filled by a later
step; ``release`` and ``channel`` likewise.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from bakar import detach_records, feed_describe, feed_filter, observability
from bakar.commands import _feed_refresh as refresh

if TYPE_CHECKING:
    from collections.abc import Callable

CHUNK_SIZE = 1024 * 1024
VENDOR_FAMILIES = ("nxp", "ti")
_ENV_KEYS = ("BAKAR_DISPATCH_KAS", "BAKAR_DISPATCH_WORKSPACE")

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


@dataclass(frozen=True)
class DescribeDeps:
    """Seams for tests; the defaults read the real run registry and lock."""

    run_dir_for_run: Callable[[str], Path | None] = detach_records.run_dir_for_run
    lock_probe: Callable[[Path], LockProbe] = probe_lock


def describe_build(
    run_id: str, recipes: tuple[str, ...], *, deps: DescribeDeps | None = None
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
    if tmpdir is None:
        raise DescribeError(f"run {run_id} recorded no build output location, so there is nothing to describe")
    _require_dispatch_env(run_dir, run_id)
    live, live_reason = _liveness(run_dir, deps)
    deploy_root = tmpdir / "deploy"
    files = _files(run_id, tmpdir, deploy_root, recipes)
    return _document(
        run_id,
        state,
        exit_code,
        machine=machine if isinstance(machine, str) else "",
        tmpdir=tmpdir,
        live=live,
        live_reason=live_reason,
        files=files,
    )


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
) -> feed_describe.BuildDescription:
    return feed_describe.BuildDescription(
        schema_version=feed_describe.SCHEMA_VERSION,
        run_id=run_id,
        state=state,
        exit_code=exit_code,
        machine=machine,
        release="",
        channel="",
        tmpdir="" if tmpdir is None else str(tmpdir),
        deploy_dir="" if tmpdir is None else str(tmpdir / "deploy"),
        live=live,
        live_reason=live_reason,
        files=files,
        entries=(),
        layers=(),
        bitbake_commit=None,
        warnings=(),
    )


def _run_state(run_dir: Path) -> tuple[str, int | None]:
    rec = observability.last_run_event(run_dir / "events.jsonl", lambda r: r.get("event") in ("run_end", "run_error"))
    if rec is None:
        return "running", None
    code = rec.get("exit_code")
    # bool is an int subclass; a boolean is not a recorded exit status.
    code = code if isinstance(code, int) and not isinstance(code, bool) else None
    return ("succeeded" if code == 0 else "failed"), code


def _require_dispatch_env(run_dir: Path, run_id: str) -> None:
    """Refuse a run whose environment snapshot lacks the two dispatch variables."""
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
) -> tuple[feed_describe.DescribedFile, ...]:
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
    paths.update(deploy_root.glob("images/*/*.testdata.json"))
    return tuple(_describe_file(deploy_root, p) for p in sorted(paths))


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
