"""Shared parallel filesystem-removal primitives.

Both ``bakar clean`` (wipe the BSP build dir) and ``bakar clean-cache`` (prune
stale sstate files) delete large amounts of data. A single-threaded
``shutil.rmtree`` serializes per-file metadata syscalls; spreading them across a
small thread pool parallelizes the real disk work. The pool is capped low
because XFS serializes metadata per allocation group - past a handful of
threads, per-AG lock contention on one device cancels the gain.

``parallel_apply`` is the generic pool+progress primitive (used by
``clean-cache``'s move-then-unlink GC). ``parallel_rmtree`` builds on it to wipe
a directory tree, choosing its parallel-deletion units by descending into the
tree's real fan-out rather than assuming a fixed depth.
"""

from __future__ import annotations

import errno
import os
import shutil
import stat
import threading
from pathlib import Path

# Thread-pool size for bulk rename/unlink/rmtree. These are I/O-bound syscalls
# that release the GIL, so a small pool parallelizes real disk work. Capped low
# because XFS serializes metadata per allocation group - past a handful of
# threads, per-AG lock contention on one device cancels the gain.
GC_WORKERS = min(8, (os.cpu_count() or 4) * 2)


def parallel_apply(items: list, fn, description: str, *, show_eta: bool = True) -> list:
    """Map *fn* over *items* across a thread pool, driving a Rich progress bar.

    Splits *items* into ``GC_WORKERS`` round-robin chunks so only a handful of
    futures exist regardless of item count, then runs each chunk on a worker
    thread. ``Progress.advance`` is thread-safe, so progress ticks as each item
    completes. Results are returned in chunk order (callers do not rely on it).

    ``show_eta=False`` drops the time-remaining column. The ETA is item-count
    based, so it is meaningless when the items are subtrees of wildly different
    size (see :func:`parallel_rmtree`), where it only ever shows a wrong number.
    """
    if not items:
        return []
    from concurrent.futures import ThreadPoolExecutor

    from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeRemainingColumn

    from bakar.commands._app import console

    workers = min(GC_WORKERS, len(items))
    chunks = [items[w::workers] for w in range(workers)]
    results: list = []
    columns = [
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
    ]
    if show_eta:
        columns.append(TimeRemainingColumn())
    with Progress(
        *columns,
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task(description, total=len(items))

        def _run_chunk(chunk: list) -> list:
            out: list = []
            for item in chunk:
                out.append(fn(item))
                progress.advance(task)
            return out

        with ThreadPoolExecutor(max_workers=workers) as ex:
            for chunk_result in ex.map(_run_chunk, chunks):
                results.extend(chunk_result)
    return results


def _direct_child_count(path: Path) -> int:
    """Direct children of *path*, or 0 if it is not an expandable directory.

    Symlinks count as 0 so the fan-out walk never follows one out of the tree.
    """
    try:
        if path.is_symlink() or not path.is_dir():
            return 0
        return sum(1 for _ in path.iterdir())
    except OSError:
        return 0


def _gather_remove_targets(root: Path, min_fanout: int) -> list[Path]:
    """Decide the parallel-deletion units by descending into the real tree.

    Starts from *root* and repeatedly expands the frontier directory with the
    most direct children - the heaviest fan-out, e.g. ``tmp/work/<arch>/<recipe>``
    - replacing it with its children, until the frontier holds at least
    *min_fanout* entries or nothing expandable remains. This adapts to the actual
    build-dir shape: a deep ``work/`` tree explodes into many recipe dirs, while a
    flat dir stays coarse. Symlinks are never expanded (treated as leaves), so the
    walk cannot escape *root*.

    The frontier is always an antichain (a node is only ever replaced by its
    direct children), so the returned targets contain no ancestor/descendant
    pair - parallel removal of them cannot race a parent against its child.
    Returned largest-first so ``parallel_apply``'s round-robin chunking spreads
    the heavy subtrees across workers.
    """
    targets: list[Path] = [root]
    # Cache per-path child counts and materialized children so an entry that
    # survives across while-passes (and the final sort) is counted once, not
    # once per pass. The winner's children are reused from kids_cache instead of
    # a second iterdir after the count.
    count_cache: dict[Path, int] = {}
    kids_cache: dict[Path, list[Path]] = {}

    def _count(path: Path) -> int:
        count = count_cache.get(path)
        if count is None:
            count = _direct_child_count(path)
            count_cache[path] = count
        return count

    while len(targets) < min_fanout:
        best_idx = -1
        best_count = 1  # require >= 2 children to bother expanding
        best_kids: list[Path] | None = None
        for i, t in enumerate(targets):
            count = _count(t)
            if count > best_count:
                kids = kids_cache.get(t)
                if kids is None:
                    try:
                        kids = list(t.iterdir())
                    except OSError:
                        continue
                    kids_cache[t] = kids
                best_idx, best_count, best_kids = i, count, kids
        if best_idx < 0 or best_kids is None:
            break
        targets[best_idx : best_idx + 1] = best_kids
    targets.sort(key=_count, reverse=True)
    return targets


RESIDUE_SAMPLE_LIMIT = 64
# POSIX lets rmdir report a non-empty directory as either.
_NOT_EMPTY_ERRNOS = frozenset({errno.ENOTEMPTY, errno.EEXIST})


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _remove_entry(parent_fd: int, name: str, onexc) -> None:
    """Remove the entry *name* under the held descriptor *parent_fd*.

    A directory is removed by descriptor, so no component is re-resolved by
    path. Anything else, a symlink included, is unlinked and never followed.
    Errors on a directory's contents go to *onexc* with paths relative to
    *parent_fd*.
    """
    try:
        st = os.lstat(name, dir_fd=parent_fd)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(st.st_mode):
        shutil.rmtree(name, dir_fd=parent_fd, onexc=onexc)
    else:
        os.unlink(name, dir_fd=parent_fd)


def parallel_rmtree(root: Path, description: str = "Removing") -> tuple[Path, OSError] | None:
    """Remove *root* and everything under it, parallelizing across subtrees.

    No-op (returns None) when *root* is absent. Picks deletion units via
    :func:`_gather_remove_targets` (adapts to the tree's real fan-out), removes
    them across the shared pool, then drops the now-empty skeleton. A failure on
    one unit never stops the others. Returns None when *root* is gone afterwards
    (or is an empty directory that only its own rmdir could not remove, e.g. a
    mount point), otherwise the first remaining path with the error that blocked
    it. The error carries ``residue_count`` (every undeletable entry seen,
    each counted once: a directory refused as non-empty only because of
    residue already counted below it is not counted again) and
    ``residue_sample`` (the first :data:`RESIDUE_SAMPLE_LIMIT` ``(path, error)``
    pairs). Only that sample of errors is retained; beyond it, memory grows
    with the number of directories holding residue, not with the number of
    undeletable files. Stored errors have their tracebacks dropped.
    """
    if not root.exists() and not root.is_symlink():
        return None

    targets = _gather_remove_targets(root, GC_WORKERS * 4)
    sample: list[tuple[Path, OSError]] = []
    count = 0
    lock = threading.Lock()
    # Each failed directory and every ancestor of a recorded entry, up to
    # root. An rmdir refused as non-empty on one of these is the consequence
    # of residue already counted below it, not a new undeletable entry.
    blocked: set[Path] = set()
    # Units the first pass left residue in. The skeleton pass re-walks them,
    # and its errors there repeat entries already counted.
    failed_units: set[Path] = set()

    def _record(path: Path, exc: OSError, func: object, unit: Path | None) -> None:
        """Count one undeletable entry, unless it only repeats known residue. Caller holds ``lock``."""
        nonlocal count
        if func is os.rmdir and exc.errno in _NOT_EMPTY_ERRNOS and path in blocked:
            return
        count += 1
        if len(sample) < RESIDUE_SAMPLE_LIMIT:
            sample.append((path, exc.with_traceback(None)))
        if unit is not None:
            failed_units.add(unit)
        node = path.parent if func is os.unlink else path
        while node not in blocked:
            blocked.add(node)
            if node == root or node == node.parent:
                break
            node = node.parent

    def _unit_on_error(unit: Path, base: Path):
        def _on_error(func, path, exc, **_kwargs) -> None:
            # Already-gone entries (a concurrent unit removed them) are not residue.
            if isinstance(exc, FileNotFoundError):
                return
            with lock:
                _record(base / path, exc, func, unit)

        return _on_error

    def _skeleton_on_error(func, path, exc, **_kwargs) -> None:
        if isinstance(exc, FileNotFoundError):
            return
        entry = Path(path)
        with lock:
            if any(entry == unit or unit in entry.parents for unit in failed_units):
                return
            _record(entry, exc, func, None)

    def _skeleton_rel_error(func, path, exc, **_kwargs) -> None:
        _skeleton_on_error(func, root / path, exc)

    def _open_parent(rel_parts: tuple[str, ...]) -> int:
        """Descriptor of a unit's parent, opened one component at a time.

        Every step is ``O_NOFOLLOW`` relative to the previous descriptor, so a
        parent swapped for a symlink after the target list was built fails here
        instead of being followed out of the tree.
        """
        cur = os.dup(root_fd)
        try:
            for part in rel_parts:
                nxt = os.open(part, _DIR_FLAGS, dir_fd=cur)
                os.close(cur)
                cur = nxt
        except BaseException:
            os.close(cur)
            raise
        return cur

    def _rm(path: Path) -> None:
        rel = path.relative_to(root).parts
        try:
            parent_fd = _open_parent(rel[:-1])
        except OSError as exc:
            if not isinstance(exc, FileNotFoundError):
                with lock:
                    _record(path, exc, None, path)
            return
        try:
            _remove_entry(parent_fd, rel[-1], _unit_on_error(path, path.parent))
        except OSError as exc:
            with lock:
                _record(path, exc, None, path)
        finally:
            os.close(parent_fd)

    if root.is_symlink() or not root.is_dir():
        # Never followed: a symlink or plain file at the root is one entry.
        try:
            root.unlink()
        except FileNotFoundError:
            return None
        except OSError as exc:
            return root, _with_residue(exc.with_traceback(None), 1, [(root, exc)])
        return None
    try:
        root_fd = os.open(root, _DIR_FLAGS)
    except OSError as exc:
        return root, _with_residue(exc.with_traceback(None), 1, [(root, exc)])
    try:
        # No ETA: deletion units are subtrees of wildly different size, so an
        # item-count time-remaining estimate is always wrong.
        parallel_apply([t for t in targets if t != root], _rm, description, show_eta=False)
        # Drop the skeleton (the now-empty parent dirs left behind by expansion).
        # A failure on a skeleton entry itself is new residue and is counted.
        try:
            names = os.listdir(root_fd)
        except OSError as exc:
            _skeleton_on_error(os.listdir, root, exc)
            names = []
        for name in names:
            try:
                _remove_entry(root_fd, name, _skeleton_rel_error)
            except OSError as exc:
                _skeleton_on_error(None, root / name, exc)
    finally:
        os.close(root_fd)
    try:
        os.rmdir(root)
    except OSError as exc:
        _skeleton_on_error(os.rmdir, root, exc)

    if not root.exists() and not root.is_symlink():
        return None
    if not root.is_symlink() and root.is_dir():
        try:
            empty = next(root.iterdir(), None) is None
        except OSError:
            empty = False
        if empty:
            return None  # only the root's own rmdir failed; the tree is gone
    for path, exc in sample:
        if path.exists() or path.is_symlink():
            return path, _with_residue(exc, count, sample)
    first = sample[0][1] if sample else OSError(f"{root} still exists after removal")
    return root, _with_residue(first, count, sample)


def _with_residue(exc: OSError, count: int, sample: list[tuple[Path, OSError]]) -> OSError:
    exc.residue_count = count  # ty: ignore[unresolved-attribute]
    exc.residue_sample = list(sample)  # ty: ignore[unresolved-attribute]
    return exc
