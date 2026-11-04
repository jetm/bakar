"""Selection and transport for pushing the download cache to the shared hub.

Only top-level regular files that carry a BitBake ``.done`` stamp are pushed.
Symbolic links, directory siblings (git clone stamps), ``.tmp``/``.lock``
files and anything under subdirectories such as ``git2`` yield nothing. The
transfer is additive: ``--ignore-existing`` and no deletion, in-place update
or source removal, so a push can never damage the hub or the local cache.
"""

from __future__ import annotations

import os
import shlex
import stat
import subprocess
import tempfile
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from bakar.steps.remote_dispatch import RSYNC_IO_TIMEOUT_SECONDS, SSH_BOUND_OPTS

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from bakar.steps.cache_queue import PushQueue

_STAMP_SUFFIX = ".done"
_FULL_MARKER = "No space left on device"
_UNREACHABLE_CODES = frozenset({255, 10, 12, 30, 35})
_FULL_CODE = 11


class Outcome(Enum):
    OK = "ok"
    FULL = "full"
    UNREACHABLE = "unreachable"
    FAILED = "failed"


def _is_regular(path: Path) -> bool:
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def eligible_pairs(dl_dir: Path, stamp_names: Iterable[str]) -> list[str]:
    """Return ``[file, file.done]`` for each stamp whose pair is pushable."""
    out: list[str] = []
    for name in stamp_names:
        if not name.endswith(_STAMP_SUFFIX) or "/" in name or name.startswith("."):
            continue
        base = name[: -len(_STAMP_SUFFIX)]
        if not base:
            continue
        if _is_regular(dl_dir / base) and _is_regular(dl_dir / name):
            out.extend([base, name])
    return out


def scan_candidates(dl_dir: Path) -> list[str]:
    """Apply :func:`eligible_pairs` to the top level of ``dl_dir`` only."""
    with os.scandir(dl_dir) as it:
        stamps = sorted(e.name for e in it if e.name.endswith(_STAMP_SUFFIX))
    return eligible_pairs(dl_dir, stamps)


def push_ssh_argv(key: Path) -> list[str]:
    return [
        "ssh",
        "-i",
        str(key),
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "BatchMode=yes",
        *SSH_BOUND_OPTS,
    ]


def _rsync_base(dl_dir: Path, target: str, key: Path, list_file: Path) -> list[str]:
    return [
        "rsync",
        "--times",
        "--ignore-existing",
        f"--files-from={list_file}",
        f"--timeout={RSYNC_IO_TIMEOUT_SECONDS}",
        "-e",
        shlex.join(push_ssh_argv(key)),
    ]


def _endpoints(dl_dir: Path, target: str) -> list[str]:
    return [f"{str(dl_dir).rstrip('/')}/", f"{target.rstrip('/')}/"]


def build_push_argv(dl_dir: Path, target: str, key: Path, list_file: Path) -> list[str]:
    return [*_rsync_base(dl_dir, target, key, list_file), *_endpoints(dl_dir, target)]


def build_reconcile_argv(dl_dir: Path, target: str, key: Path, list_file: Path) -> list[str]:
    return [
        *_rsync_base(dl_dir, target, key, list_file),
        "--dry-run",
        "--out-format=%n",
        *_endpoints(dl_dir, target),
    ]


def classify_rsync(returncode: int, stderr: str) -> Outcome:
    if returncode == 0:
        return Outcome.OK
    if returncode == _FULL_CODE or _FULL_MARKER in stderr:
        return Outcome.FULL
    if returncode in _UNREACHABLE_CODES:
        return Outcome.UNREACHABLE
    return Outcome.FAILED


def _still_eligible(dl_dir: Path, name: str) -> bool:
    stamp = name if name.endswith(_STAMP_SUFFIX) else name + _STAMP_SUFFIX
    return name in eligible_pairs(dl_dir, [stamp])


def drain_once(  # noqa: PLR0913 - signature fixed by the spec
    queue: PushQueue,
    dl_dir: Path,
    target: str,
    key: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    batch: int = 500,
) -> Outcome:
    """Push up to ``batch`` queued names once; update the queue by outcome."""
    names = queue.pending(batch)
    stale = [n for n in names if not _still_eligible(dl_dir, n)]
    if stale:
        queue.remove(stale)
    live = [n for n in names if n not in set(stale)]
    if not live:
        return Outcome.OK
    with tempfile.NamedTemporaryFile("w", suffix=".list", delete=False) as fh:
        fh.write("\n".join(live) + "\n")
        list_path = Path(fh.name)
    try:
        proc = runner(
            build_push_argv(dl_dir, target, key, list_path),
            capture_output=True,
            text=True,
            check=False,
            start_new_session=True,
        )
    finally:
        list_path.unlink(missing_ok=True)
    stderr = proc.stderr or ""
    outcome = classify_rsync(proc.returncode, stderr)
    if outcome is Outcome.OK:
        queue.remove(live)
        queue.set_paused(None)
    elif outcome is Outcome.FULL:
        queue.set_paused("target disk full")
    else:
        queue.record_failure(live, stderr.strip()[-200:] or f"rsync exit {proc.returncode}")
    return outcome
