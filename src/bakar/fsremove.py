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

import os
import shutil
from typing import TYPE_CHECKING

if TYPE_CHECKING:
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
    while len(targets) < min_fanout:
        best_idx = -1
        best_count = 1  # require >= 2 children to bother expanding
        best_kids: list[Path] | None = None
        for i, t in enumerate(targets):
            count = _direct_child_count(t)
            if count > best_count:
                try:
                    kids = list(t.iterdir())
                except OSError:
                    continue
                best_idx, best_count, best_kids = i, count, kids
        if best_idx < 0 or best_kids is None:
            break
        targets[best_idx : best_idx + 1] = best_kids
    targets.sort(key=_direct_child_count, reverse=True)
    return targets


def parallel_rmtree(root: Path, description: str = "Removing") -> None:
    """Remove *root* and everything under it, parallelizing across subtrees.

    No-op when *root* is absent. Picks deletion units via
    :func:`_gather_remove_targets` (adapts to the tree's real fan-out), removes
    them across the shared pool, then drops the now-empty skeleton. A failure on
    any single unit is skipped, not fatal.
    """
    if not root.exists() and not root.is_symlink():
        return

    targets = _gather_remove_targets(root, GC_WORKERS * 4)

    def _rm(path: Path) -> None:
        try:
            if path.is_symlink() or not path.is_dir():
                path.unlink()
            else:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass

    # No ETA: deletion units are subtrees of wildly different size, so an
    # item-count time-remaining estimate is always wrong.
    parallel_apply(targets, _rm, description, show_eta=False)
    # Drop the skeleton (the now-empty parent dirs left behind by expansion).
    shutil.rmtree(root, ignore_errors=True)
