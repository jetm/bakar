"""Plan the removal of downloads that are redundant with the hub or a clone.

This module holds the planning half of ``bakar cache prune-downloads``: it
inspects a downloads directory and decides which items could be removed, and
deletes nothing. Only the top level of ``dl_dir`` and of ``dl_dir/git2`` is
listed, no symbolic link is ever followed, and the only recursion is the size
sum of a clone that was already selected.

Three classes are planned:

* ``hub``: a top-level payload plus its ``.done`` stamp that the hub mirror
  serves with the same length.
* ``tarball``: a ``git2_<n>.tar.gz`` mirror tarball whose extracted clone
  ``git2/<n>`` is complete beside it.
* ``clone``: a ``git2/<n>`` clone whose own stamp is older than a threshold.
"""

from __future__ import annotations

import fcntl
import os
import shutil
import stat
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from bakar import probes
from bakar.steps.cache_queue import is_unsafe_name

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

Kind = Literal["hub", "tarball", "clone"]

_STAMP_SUFFIX = ".done"
_SKIP_SUFFIXES = (".tmp", ".lock", _STAMP_SUFFIX)
_GIT_MIRROR_PREFIXES = ("git2_", "gitshallow_")
_TARBALL_PREFIX = "git2_"
_TARBALL_SUFFIX = ".tar.gz"
_GIT2_DIR = "git2"
_HEAD_TIMEOUT_S = 2.0
_MAX_CONSECUTIVE_NO_ANSWER = 3
_SECONDS_PER_DAY = 86400

STATUS_EXAMINED = "examined"
STATUS_UNAVAILABLE = "unavailable"
STATUS_NOT_REQUESTED = "not requested"


@dataclass(frozen=True)
class Item:
    """One removable unit: ``paths`` lists the stamp first, then the payload."""

    kind: Kind
    name: str
    paths: tuple[Path, ...]
    size: int
    lock: Path
    stat_key: tuple[int, int]


@dataclass(frozen=True)
class Plan:
    """The outcome of planning: items to remove, items kept and why, per-class status."""

    items: dict[str, list[Item]] = field(default_factory=lambda: {"hub": [], "tarball": [], "clone": []})
    kept: dict[tuple[str, str], int] = field(default_factory=dict)
    status: dict[str, str] = field(
        default_factory=lambda: {"hub": STATUS_EXAMINED, "tarball": STATUS_EXAMINED, "clone": STATUS_EXAMINED}
    )

    def keep(self, kind: str, reason: str, count: int = 1) -> None:
        if count > 0:
            self.kept[kind, reason] = self.kept.get((kind, reason), 0) + count


def _lstat_regular(path: Path | str) -> os.stat_result | None:
    """The ``lstat`` of ``path`` when it is a regular file (never a link), else None."""
    try:
        st = os.lstat(path)
    except OSError:
        return None
    return st if stat.S_ISREG(st.st_mode) else None


def _is_real_dir(path: Path) -> bool:
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


def _top_level_names(directory: Path) -> list[str]:
    try:
        with os.scandir(directory) as it:
            return sorted(e.name for e in it)
    except OSError:
        return []


def _clone_size(clone: Path) -> int:
    """Sum of regular-file ``lstat`` sizes under ``clone``, never following a link."""
    total = 0
    for root, _dirs, files in os.walk(clone, followlinks=False):
        for fname in files:
            st = _lstat_regular(os.path.join(root, fname))
            if st is not None:
                total += st.st_size
    return total


def _hub_candidates(dl_dir: Path) -> list[tuple[str, os.stat_result]]:
    out: list[tuple[str, os.stat_result]] = []
    for name in _top_level_names(dl_dir):
        if is_unsafe_name(name) or name.startswith(_GIT_MIRROR_PREFIXES) or name.endswith(_SKIP_SUFFIXES):
            continue
        payload = _lstat_regular(dl_dir / name)
        if payload is None or _lstat_regular(dl_dir / (name + _STAMP_SUFFIX)) is None:
            continue
        out.append((name, payload))
    return out


def _plan_hub(
    dl_dir: Path,
    mirror_url: str,
    plan: Plan,
    head: Callable[[str, float], probes.HeadInfo | None],
) -> None:
    base_url = mirror_url.rstrip("/")
    candidates = _hub_candidates(dl_dir)
    verified: list[Item] = []
    no_answer = 0
    for index, (name, st) in enumerate(candidates):
        if no_answer >= _MAX_CONSECUTIVE_NO_ANSWER:
            # The hub has gone quiet: keep this and every later candidate, and
            # withdraw what was already verified so the class removes nothing.
            plan.keep("hub", "unverified", len(candidates) - index + len(verified))
            verified.clear()
            return
        url = f"{base_url}/{urllib.parse.quote(name)}"
        info = head(url, _HEAD_TIMEOUT_S)
        if info is None:
            no_answer += 1
            plan.keep("hub", "unverified")
            continue
        no_answer = 0
        if not 200 <= info.status < 300:
            plan.keep("hub", "hub lacks it" if info.status == 404 else f"hub status {info.status}")
            continue
        if info.content_length is None:
            plan.keep("hub", "no length")
            continue
        if info.content_length != st.st_size:
            plan.keep("hub", "size mismatch")
            continue
        stamp_info = head(url + _STAMP_SUFFIX, _HEAD_TIMEOUT_S)
        if stamp_info is None:
            no_answer += 1
            plan.keep("hub", "unverified")
            continue
        if not 200 <= stamp_info.status < 300:
            plan.keep("hub", "hub lacks stamp")
            continue
        verified.append(
            Item(
                kind="hub",
                name=name,
                paths=(dl_dir / (name + _STAMP_SUFFIX), dl_dir / name),
                size=st.st_size,
                lock=dl_dir / (name + ".lock"),
                stat_key=(st.st_size, st.st_mtime_ns),
            )
        )
    if no_answer >= _MAX_CONSECUTIVE_NO_ANSWER:
        plan.keep("hub", "unverified", len(verified))
        return
    plan.items["hub"].extend(verified)


def _plan_tarballs(dl_dir: Path, plan: Plan) -> None:
    git2 = dl_dir / _GIT2_DIR
    for name in _top_level_names(dl_dir):
        if not (name.startswith(_TARBALL_PREFIX) and name.endswith(_TARBALL_SUFFIX)) or is_unsafe_name(name):
            continue
        st = _lstat_regular(dl_dir / name)
        if st is None or _lstat_regular(dl_dir / (name + _STAMP_SUFFIX)) is None:
            continue
        clone_name = name[len(_TARBALL_PREFIX) : -len(_TARBALL_SUFFIX)]
        clone = git2 / clone_name
        if (
            is_unsafe_name(clone_name)
            or not _is_real_dir(clone)
            or _lstat_regular(clone / "HEAD") is None
            or _lstat_regular(git2 / (clone_name + _STAMP_SUFFIX)) is None
        ):
            plan.keep("tarball", "no valid clone")
            continue
        plan.items["tarball"].append(
            Item(
                kind="tarball",
                name=name,
                paths=(dl_dir / (name + _STAMP_SUFFIX), dl_dir / name),
                size=st.st_size,
                lock=git2 / (clone_name + ".lock"),
                stat_key=(st.st_size, st.st_mtime_ns),
            )
        )


def _plan_clones(dl_dir: Path, older_than_days: int, now: float, plan: Plan) -> None:
    git2 = dl_dir / _GIT2_DIR
    cutoff = now - older_than_days * _SECONDS_PER_DAY
    for name in _top_level_names(git2):
        if is_unsafe_name(name) or name.endswith(_SKIP_SUFFIXES):
            continue
        clone = git2 / name
        if not _is_real_dir(clone):
            continue
        stamp = _lstat_regular(git2 / (name + _STAMP_SUFFIX))
        if stamp is None:
            plan.keep("clone", "no stamp")
            continue
        if stamp.st_mtime >= cutoff:
            plan.keep("clone", "too recent")
            continue
        st = os.lstat(clone)
        plan.items["clone"].append(
            Item(
                kind="clone",
                name=name,
                paths=(git2 / (name + _STAMP_SUFFIX), clone),
                size=_clone_size(clone),
                lock=git2 / (name + ".lock"),
                stat_key=(st.st_size, st.st_mtime_ns),
            )
        )


def plan_prune(
    dl_dir: Path,
    *,
    mirror_url: str | None,
    clone_older_than_days: int | None,
    head: Callable[[str, float], probes.HeadInfo | None] = probes.head_info,
    now: float | None = None,
) -> Plan:
    """Decide what could be removed from ``dl_dir``; removes nothing."""
    plan = Plan()
    if mirror_url:
        _plan_hub(dl_dir, mirror_url, plan, head)
    else:
        plan.status["hub"] = STATUS_UNAVAILABLE
    _plan_tarballs(dl_dir, plan)
    if clone_older_than_days is None:
        plan.status["clone"] = STATUS_NOT_REQUESTED
    else:
        _plan_clones(dl_dir, clone_older_than_days, time.time() if now is None else now, plan)
    return plan


REASON_IN_USE = "in use"
REASON_CHANGED = "changed since planned"
REASON_REMOVE_FAILED = "remove failed"
REASON_LOCK_UNAVAILABLE = "lock unavailable"

_KINDS: tuple[Kind, ...] = ("hub", "tarball", "clone")


@dataclass
class ApplyResult:
    """What ``apply_plan`` removed and kept, per class (kept reasons are apply-time only)."""

    removed: dict[str, int] = field(default_factory=lambda: dict.fromkeys(_KINDS, 0))
    removed_size: dict[str, int] = field(default_factory=lambda: dict.fromkeys(_KINDS, 0))
    kept: dict[tuple[str, str], int] = field(default_factory=dict)

    def keep(self, kind: str, reason: str) -> None:
        self.kept[kind, reason] = self.kept.get((kind, reason), 0) + 1


def _remove(path: Path, *, tree: bool = False) -> None:
    """The only place this module deletes anything; a path already gone is fine."""
    try:
        if tree:
            shutil.rmtree(path)
        else:
            os.unlink(path)
    except FileNotFoundError:
        pass


def _payload_unchanged(item: Item, dl_dir: Path) -> bool:
    """Re-check, under the lock, that the payload is still what was planned."""
    payload = item.paths[-1]
    parent = dl_dir / _GIT2_DIR if item.kind == "clone" else dl_dir
    if payload.parent != parent or (item.kind == "clone" and not _is_real_dir(parent)):
        return False
    try:
        st = os.lstat(payload)
    except OSError:
        return False
    wanted = stat.S_ISDIR if item.kind == "clone" else stat.S_ISREG
    return wanted(st.st_mode) and (st.st_size, st.st_mtime_ns) == item.stat_key


def _apply_item(item: Item, dl_dir: Path, result: ApplyResult) -> None:
    try:
        fd = os.open(item.lock, os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        result.keep(item.kind, REASON_LOCK_UNAVAILABLE)
        return
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            result.keep(item.kind, REASON_IN_USE)
            return
        if not _payload_unchanged(item, dl_dir):
            result.keep(item.kind, REASON_CHANGED)
            return
        stamp, payload = item.paths
        try:
            _remove(stamp)
            _remove(payload, tree=item.kind == "clone")
        except OSError:
            # The stamp goes first, so a failure here leaves a payload with no
            # stamp, which BitBake treats as not downloaded and fetches again.
            result.keep(item.kind, REASON_REMOVE_FAILED)
            return
        # Unlinked while still locked, as BitBake does on release.
        _remove(item.lock)
        result.removed[item.kind] += 1
        result.removed_size[item.kind] += item.size
    finally:
        os.close(fd)


def apply_plan(plan: Plan, dl_dir: Path) -> ApplyResult:
    """Remove the planned items, skipping any that are locked or changed."""
    result = ApplyResult()
    for kind in _KINDS:
        for item in plan.items[kind]:
            _apply_item(item, dl_dir, result)
    return result


def _fmt_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024:
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


def render_report(plan: Plan, result: ApplyResult | None, apply: bool) -> str:  # noqa: FBT001
    """One block per class, then a closing dry-run or applied line."""
    lines: list[str] = []
    for kind in _KINDS:
        lines.append(f"{kind}: {plan.status[kind]}")
        if result is None:
            count = len(plan.items[kind])
            size = sum(i.size for i in plan.items[kind])
            verb = "would remove"
            kept = dict(plan.kept)
        else:
            count = result.removed[kind]
            size = result.removed_size[kind]
            verb = "removed"
            kept = dict(plan.kept)
            for key, n in result.kept.items():
                kept[key] = kept.get(key, 0) + n
        lines.append(f"  {verb} {count} item(s), {_fmt_size(size)}")
        lines.extend(f"  kept {n}: {reason}" for (k, reason), n in sorted(kept.items()) if k == kind)
    lines.append("applied" if apply else "dry run: nothing was removed (pass --apply to remove)")
    return "\n".join(lines) + "\n"
