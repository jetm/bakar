"""Selection and transport for pushing the download cache to the shared hub.

Only top-level regular files that carry a BitBake ``.done`` stamp are pushed.
Symbolic links, directory siblings (git clone stamps), ``.tmp``/``.lock``
files and anything under subdirectories such as ``git2`` yield nothing. The
transfer is additive: ``--ignore-existing`` and no deletion, in-place update
or source removal, so a push can never damage the hub or the local cache.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shlex
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import urllib.parse
from enum import Enum
from pathlib import Path
from queue import Empty, SimpleQueue
from typing import TYPE_CHECKING, Any

from bakar import probes
from bakar.steps.cache_queue import is_unsafe_name
from bakar.steps.remote_dispatch import RSYNC_IO_TIMEOUT_SECONDS, SSH_BOUND_OPTS

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from bakar.steps.cache_queue import PushQueue

_STAMP_SUFFIX = ".done"
_SKIP_SUFFIXES = (".tmp", ".lock")
# BitBake git mirror tarballs (full and shallow): the hub PREMIRRORS no longer serves git, so nobody consumes them.
_GIT_MIRROR_PREFIXES = ("git2_", "gitshallow_")
# Per-drain wall-clock budget for the hub pre-push checks; the rest of the batch is pushed unchecked.
_HUB_CHECK_BUDGET_S = 10.0
_HUB_CHECK_TIMEOUT_S = 2.0
_ISOLATE_CALL_BUDGET = 48
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
        if not name.endswith(_STAMP_SUFFIX) or is_unsafe_name(name):
            continue
        base = name[: -len(_STAMP_SUFFIX)]
        if not base or base.endswith(_SKIP_SUFFIXES):
            continue
        if base.startswith(_GIT_MIRROR_PREFIXES):
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
        "--from0",
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
        "-8",
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
    stop: threading.Event | None = None,
    mirror_url: str | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> Outcome:
    """Push up to ``batch`` queued names once; update the queue by outcome.

    With ``mirror_url`` set, a pair the hub mirror already serves is dropped from
    the queue without a transfer: a pusher premirror-fetches many files from the
    hub, and pushing them back is wasted work.
    """
    names = queue.pending(batch)
    stale = [n for n in names if not _still_eligible(dl_dir, n)]
    if stale:
        queue.remove(stale)
    live = [n for n in names if n not in set(stale)]
    if live and mirror_url:
        on_hub = _served_by_hub(live, mirror_url, stop=stop, clock=clock)
        if on_hub:
            queue.remove(on_hub)
            live = [n for n in live if n not in set(on_hub)]
    if not live:
        return Outcome.OK
    outcome, error = _push_names(live, dl_dir, target, key, runner)
    if outcome is not Outcome.OK:
        logger.warning("push of %d file(s) to %s failed (%s): %s", len(live), target, outcome.name.lower(), error)
    if outcome is Outcome.OK:
        queue.remove(live)
        queue.set_paused(None)
    elif outcome is Outcome.FULL:
        queue.set_paused("target disk full")
    elif outcome is Outcome.FAILED and len(_group_pairs(live)) > 1:
        return _isolate_failures(queue, live, dl_dir, target, key, runner, error, stop)
    else:
        queue.record_failure(live, error)
    return outcome


def _served_by_hub(
    names: list[str],
    mirror_url: str,
    *,
    stop: threading.Event | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> list[str]:
    """Return the queued names whose pair the hub mirror already serves.

    A pair counts as served only when both the payload and its ``.done`` stamp
    answer HEAD 2xx: a payload alone can be a truncated or leftover file, while
    the stamp exists only after a completed, verified fetch. A payload that
    answers anything else skips the stamp check.

    Checking ends, and everything unchecked is pushed, when a check gets no
    answer at all (error or timeout, as opposed to a clean 404), when the
    ``_HUB_CHECK_BUDGET_S`` budget is spent, or when ``stop`` is set.
    """
    base_url = mirror_url.rstrip("/")
    deadline = clock() + _HUB_CHECK_BUDGET_S
    served: list[str] = []

    def head(name: str) -> int | bool | None:
        """HEAD status, or False when checking must end before this request."""
        if (stop is not None and stop.is_set()) or clock() >= deadline:
            return False
        return probes.head_status(f"{base_url}/{urllib.parse.quote(name)}", _HUB_CHECK_TIMEOUT_S)

    for group in _group_pairs(names):
        base = group[0][: -len(_STAMP_SUFFIX)] if group[0].endswith(_STAMP_SUFFIX) else group[0]
        payload = head(base)
        if payload is False or payload is None:
            break
        if not 200 <= payload < 300:
            continue
        stamp = head(base + _STAMP_SUFFIX)
        if stamp is False or stamp is None:
            break
        if 200 <= stamp < 300:
            served.extend(group)
    return served


def _group_pairs(names: list[str]) -> list[list[str]]:
    """Group queued names by file so ``x`` and ``x.done`` stay together."""
    groups: dict[str, list[str]] = {}
    for name in names:
        base = name[: -len(_STAMP_SUFFIX)] if name.endswith(_STAMP_SUFFIX) else name
        groups.setdefault(base, []).append(name)
    return list(groups.values())


def _push_names(
    names: list[str],
    dl_dir: Path,
    target: str,
    key: Path,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> tuple[Outcome, str]:
    with tempfile.NamedTemporaryFile("wb", suffix=".list", delete=False) as fh:
        fh.write(b"".join(os.fsencode(n) + b"\0" for n in names))
        list_path = Path(fh.name)
    try:
        proc = runner(
            build_push_argv(dl_dir, target, key, list_path),
            capture_output=True,
            text=True,
            errors="surrogateescape",
            check=False,
            start_new_session=True,
        )
    finally:
        list_path.unlink(missing_ok=True)
    stderr = proc.stderr or ""
    return classify_rsync(proc.returncode, stderr), stderr.strip()[-200:] or f"rsync exit {proc.returncode}"


def _isolate_failures(  # noqa: PLR0913 - mirrors drain_once
    queue: PushQueue,
    live: list[str],
    dl_dir: Path,
    target: str,
    key: Path,
    runner: Callable[..., subprocess.CompletedProcess[str]],
    error: str,
    stop: threading.Event | None,
) -> Outcome:
    """Bisect a failed batch so one bad file cannot block the rest.

    Depth first: every half that succeeds leaves the queue, every half that fails
    is split again, and a lone failing pair is recorded. One bad pair among N
    costs about 2*log2(N)+1 transfers. ``_ISOLATE_CALL_BUDGET`` caps the rsync
    runs per drain so a systemic failure cannot cost O(N); pairs not yet proven
    good when it runs out are recorded once. An unreachable or full target stops
    immediately and leaves every untried pair untouched.
    """
    budget = _ISOLATE_CALL_BUDGET
    unresolved: list[str] = []
    last_error = error

    def bisect(groups: list[list[str]], group_error: str) -> Outcome:
        nonlocal budget, last_error
        if len(groups) == 1:
            logger.warning("giving up on %s for now: %s", groups[0][0], group_error)
            queue.record_failure(groups[0], group_error)
            return Outcome.FAILED
        mid = len(groups) // 2
        result = Outcome.OK
        for half in (groups[:mid], groups[mid:]):
            if stop is not None and stop.is_set():
                break
            names = [n for g in half for n in g]
            if budget <= 0:
                unresolved.extend(names)
                result = Outcome.FAILED
                continue
            budget -= 1
            outcome, half_error = _push_names(names, dl_dir, target, key, runner)
            if outcome is Outcome.OK:
                queue.remove(names)
            elif outcome is Outcome.FULL:
                queue.set_paused("target disk full")
                return outcome
            elif outcome is Outcome.UNREACHABLE:
                return outcome
            else:
                last_error = half_error
                sub = bisect(half, half_error)
                if sub in (Outcome.FULL, Outcome.UNREACHABLE):
                    return sub
                result = Outcome.FAILED
        return result

    result = bisect(_group_pairs(live), error)
    if unresolved and result is Outcome.FAILED:
        queue.record_failure(unresolved, last_error)
    if result is Outcome.OK and not (stop is not None and stop.is_set()):
        queue.set_paused(None)
    return result


_BACKOFF_CAP_S = 300.0
_FULL_RETRY_S = 300.0
_INOTIFY_RESTART_S = 5.0
_IDLE_SLEEP_S = 0.2
_REAP_TIMEOUT_S = 2.0

logger = logging.getLogger(__name__)


def check_prerequisites(dl_dir: Path | None, target: str | None, key: Path) -> str | None:
    """Return a message for the first missing prerequisite, else None."""
    if not target:
        return "cache_push_target is not set"
    if dl_dir is None:
        return "the downloads directory (dl_dir) is not set"
    if shutil.which("inotifywait") is None:
        return "inotifywait is not installed (install inotify-tools)"
    if not key.is_file():
        return f"push key file not found: {key}"
    dl_dir.mkdir(parents=True, exist_ok=True)
    return None


def inotify_argv(dl_dir: Path) -> list[str]:
    """Non-recursive watch of the top level of ``dl_dir`` only."""
    return ["inotifywait", "-m", "-q", "-e", "close_write", "--format", "%f", str(dl_dir)]


def _valid_name(name: str) -> bool:
    return not is_unsafe_name(name)


def _reconcile(  # noqa: PLR0913 - mirrors the watch() injection points
    queue: PushQueue,
    dl_dir: Path,
    target: str,
    key: Path,
    runner: Callable[..., subprocess.CompletedProcess[str]],
    now: float,
) -> Outcome:
    try:
        candidates = scan_candidates(dl_dir)
    except OSError as exc:
        logger.warning("cannot scan %s: %s; retrying next interval", dl_dir, exc)
        return Outcome.OK
    with tempfile.NamedTemporaryFile("wb", suffix=".list", delete=False) as fh:
        fh.write(b"".join(os.fsencode(n) + b"\0" for n in candidates))
        list_path = Path(fh.name)
    try:
        proc = runner(
            build_reconcile_argv(dl_dir, target, key, list_path),
            capture_output=True,
            text=True,
            errors="surrogateescape",
            check=False,
            start_new_session=True,
        )
    finally:
        list_path.unlink(missing_ok=True)
    outcome = classify_rsync(proc.returncode, proc.stderr or "")
    if outcome is Outcome.OK:
        names = (proc.stdout or "").split("\n")
        queue.enqueue([n for n in names if _valid_name(n)])
        queue.mark_reconciled(now)
    else:
        detail = (proc.stderr or "").strip()[-200:] or f"rsync exit {proc.returncode}"
        logger.warning("reconcile against %s failed (%s): %s", target, outcome.name.lower(), detail)
    return outcome


def _read_lines(stream: Iterable[str], out: SimpleQueue[str | None]) -> None:
    try:
        for line in stream:
            out.put(line.rstrip("\n"))
    finally:
        out.put(None)


def _reap(proc: Any) -> None:
    """Terminate and wait for ``proc`` so it never lingers as a zombie."""
    if proc is None:
        return
    with contextlib.suppress(Exception):
        proc.terminate()
    try:
        proc.wait(timeout=_REAP_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(Exception):
            proc.kill()
            proc.wait(timeout=_REAP_TIMEOUT_S)
    except Exception:
        logger.debug("could not reap inotifywait", exc_info=True)


def watch(  # noqa: PLR0913 - signature fixed by the spec
    dl_dir: Path,
    target: str,
    key: Path,
    *,
    queue: PushQueue,
    stop_event: threading.Event | None = None,
    settle_s: float = 2.0,
    reconcile_every_s: float = 600.0,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    popen: Callable[..., Any] = subprocess.Popen,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    mirror_url: str | None = None,
) -> None:
    """Watch ``dl_dir`` and push finished downloads until ``stop_event`` is set."""
    stop = stop_event if stop_event is not None else threading.Event()
    lines: SimpleQueue[str | None] = SimpleQueue()
    proc: Any = None
    restart_at = 0.0
    first_undrained: float | None = None
    blocked_until = 0.0
    failures = 0
    next_reconcile = clock()

    def note_failure(outcome: Outcome, now: float) -> None:
        nonlocal blocked_until, failures
        if outcome is Outcome.FULL:
            blocked_until = now + _FULL_RETRY_S
        elif outcome in (Outcome.UNREACHABLE, Outcome.FAILED):
            blocked_until = now + min(2.0**failures, _BACKOFF_CAP_S)
            failures += 1
        else:
            failures = 0

    if queue.pending(1):
        first_undrained = clock()
    try:
        while not stop.is_set():
            now = clock()
            busy = False
            if proc is None and now >= restart_at:
                try:
                    proc = popen(
                        inotify_argv(dl_dir),
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                        text=True,
                        errors="surrogateescape",
                        start_new_session=True,
                    )
                except OSError as exc:
                    logger.warning("cannot start inotifywait: %s; retrying in %.0fs", exc, _INOTIFY_RESTART_S)
                    proc = None
                    restart_at = clock() + _INOTIFY_RESTART_S
                else:
                    threading.Thread(target=_read_lines, args=(proc.stdout, lines), daemon=True).start()
            while True:
                try:
                    line = lines.get_nowait()
                except Empty:
                    break
                busy = True
                if line is None:
                    logger.warning("inotifywait exited; restarting in %.0fs", _INOTIFY_RESTART_S)
                    _reap(proc)
                    proc = None
                    restart_at = clock() + _INOTIFY_RESTART_S
                    break
                names = eligible_pairs(dl_dir, [line])
                if names and queue.enqueue(names, now=time.time()) and first_undrained is None:
                    first_undrained = now
            if now >= next_reconcile and now >= blocked_until:
                outcome = _reconcile(queue, dl_dir, target, key, runner, time.time())
                note_failure(outcome, now)
                next_reconcile = now + reconcile_every_s
                busy = True
                if first_undrained is None and queue.pending(1):
                    first_undrained = now
            if first_undrained is not None and now - first_undrained >= settle_s and now >= blocked_until:
                outcome = drain_once(
                    queue, dl_dir, target, key, runner=runner, stop=stop, mirror_url=mirror_url, clock=clock
                )
                note_failure(outcome, now)
                first_undrained = now if queue.pending(1) else None
                busy = True
            if not busy:
                sleep(_IDLE_SLEEP_S)
    finally:
        _reap(proc)
