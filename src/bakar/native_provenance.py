"""Revision-set records and the nearest-record forecast comparison.

Foundation module: stdlib only. Snapshots the workspace's git repositories,
persists one atomic record per distinct revision set under the sstate root,
and compares a current snapshot against recorded ones. Never raises on bad
repository or record state.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

_SOURCE_ROOTS = ("sources", "layers")
_CORE_REPOS = frozenset({"bitbake", "openembedded-core", "poky"})
_RECORDS_SUBDIR = Path(".bakar") / "native-provenance"
_UNKNOWN_RELEASE = "_unknown"
_HEX_SHA = re.compile(r"[0-9a-f]{7,64}", re.IGNORECASE)
_RELEASE_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


@dataclass(frozen=True)
class Repo:
    name: str
    path: Path
    sha: str | None
    dirty: bool | None
    core: bool
    unreadable: bool


@dataclass(frozen=True)
class RepoSnapshot:
    repos: tuple[Repo, ...]


@dataclass(frozen=True)
class Record:
    release: str
    repos: dict[str, dict]
    first_seen: str
    last_seen: str
    last_node: str
    last_run_id: str
    last_outcome: str
    file: str


@dataclass(frozen=True)
class RepoDiff:
    name: str
    recorded: str | None
    current: str | None
    core: bool
    ahead: int | None
    behind: int | None


@dataclass(frozen=True)
class Forecast:
    kind: Literal["exact", "nearest", "no_records"]
    record: Record | None
    diffs: tuple[RepoDiff, ...]
    dirty: tuple[str, ...]
    unreadable: tuple[str, ...]
    core_moved: bool


def _git(repo: Path, args: list[str], timeout: float) -> str | None:
    """Run a git probe; return stdout, or None when it fails or times out."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            start_new_session=True,
        )
    except subprocess.SubprocessError, OSError:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def _probe(name: str, path: Path, probe_timeout: float) -> Repo:
    core = path.name in _CORE_REPOS
    head = _git(path, ["rev-parse", "HEAD"], probe_timeout)
    status = _git(path, ["status", "--porcelain", "--untracked-files=no"], probe_timeout)
    sha = head.strip() if head is not None else ""
    if not sha or status is None:
        return Repo(name, path, None, None, core, unreadable=True)
    return Repo(name, path, sha, bool(status.strip()), core, unreadable=False)


def snapshot_repos(
    workspace: Path, bsp_root: Path, *, probe_timeout: float, total_budget: float = 60.0
) -> RepoSnapshot:
    """Snapshot every git repository under the workspace and BSP root.

    Names are unique: a basename shared by several checkouts is replaced by the
    checkout's path relative to the workspace. Once ``total_budget`` seconds are
    spent, remaining repositories are recorded unreadable without running git.
    """
    parents: list[Path] = [workspace]
    for base in (workspace, bsp_root):
        parents.extend(base / root for root in _SOURCE_ROOTS)
    seen: set[Path] = set()
    found: list[Path] = []
    for parent in parents:
        try:
            if not parent.is_dir():
                continue
            entries = sorted(parent.iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                if not entry.is_dir() or not (entry / ".git").exists():
                    continue
                resolved = entry.resolve()
            except OSError:
                continue
            if resolved in seen:
                continue
            seen.add(resolved)
            found.append(entry)
    counts = Counter(p.name for p in found)
    start = time.monotonic()
    repos: list[Repo] = []
    for entry in found:
        name = entry.name if counts[entry.name] == 1 else Path(os.path.relpath(entry, workspace)).as_posix()
        if time.monotonic() - start >= total_budget:
            repos.append(Repo(name, entry, None, None, entry.name in _CORE_REPOS, unreadable=True))
        else:
            repos.append(_probe(name, entry, probe_timeout))
    return RepoSnapshot(repos=tuple(repos))


def _digest(triples: list[tuple[str, str, bool]]) -> str:
    payload = json.dumps(sorted(triples), separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def revision_set_digest(snapshot: RepoSnapshot) -> str:
    """First 16 hex chars of sha256 over sorted (name, sha, dirty) of readable repos."""
    return _digest([(r.name, r.sha or "", bool(r.dirty)) for r in snapshot.repos if not r.unreadable])


def _record_digest(repos: dict[str, dict]) -> str:
    return _digest([(n, str(v.get("sha") or ""), bool(v.get("dirty"))) for n, v in repos.items()])


def _stamp(now: datetime) -> str:
    return now.strftime("%Y-%m-%dT%H:%M:%S%z")


def _release_dir(release_key: str | None) -> str:
    """Directory name for a release key; anything that is not one plain component maps to ``_unknown``."""
    if release_key and _RELEASE_COMPONENT.fullmatch(release_key):
        return release_key
    return _UNKNOWN_RELEASE


def write_record(  # noqa: PLR0913
    sstate_dir: Path,
    release_key: str | None,
    snapshot: RepoSnapshot,
    *,
    node: str,
    run_id: str,
    outcome: str,
    now: datetime,
) -> Path | None:
    """Publish the snapshot's revision-set record atomically; None if nothing to record."""
    readable = [r for r in snapshot.repos if not r.unreadable]
    if not readable:
        return None
    digest = revision_set_digest(snapshot)
    release = _release_dir(release_key)
    base = sstate_dir / _RECORDS_SUBDIR
    root = base / release
    target = root / f"{digest}.json"
    try:
        sstate_dir.mkdir(parents=True, exist_ok=True)
        # Records are readable by peers (a directory lacking r-x for group/other is
        # widened, never narrowed). Creating records under another uid needs a
        # group-writable shared directory provisioned by the operator. A symlink at
        # any level is refused so a planted link cannot redirect writes or chmods.
        for d in (sstate_dir / ".bakar", base, root):
            if d.is_symlink():
                return None
            d.mkdir(exist_ok=True)
            if d.is_symlink():
                return None
            mode = d.stat().st_mode & 0o7777
            if mode & 0o755 != 0o755:
                try:
                    os.chmod(d, mode | 0o755)
                except OSError:
                    pass
        stamp = _stamp(now)
        first_seen = stamp
        if target.is_file():
            try:
                prior = json.loads(target.read_text(encoding="utf-8"))
                first_seen = str(prior.get("first_seen") or stamp)
            except OSError, ValueError, AttributeError:
                pass
        payload = json.dumps(
            {
                "schema": 1,
                "release": release,
                "repos": {r.name: {"sha": r.sha, "dirty": r.dirty, "core": r.core} for r in readable},
                "first_seen": first_seen,
                "last_seen": stamp,
                "last_node": node,
                "last_run_id": run_id,
                "last_outcome": outcome,
            },
            indent=2,
            sort_keys=True,
        )
        # Leading dot + ".json.tmp" keeps an orphan out of the reader's "*.json" glob.
        fd, tmp_name = tempfile.mkstemp(prefix=f".{digest}.", suffix=".json.tmp", dir=root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload + "\n")
            os.chmod(tmp_name, 0o644)
            os.replace(tmp_name, target)
        except OSError:
            Path(tmp_name).unlink(missing_ok=True)
            raise
    except OSError:
        return None
    return target


def load_records(sstate_dir: Path, release_key: str | None) -> tuple[list[Record], list[str]]:
    """Load every record for the release; bad files are named in the second list."""
    root = sstate_dir / _RECORDS_SUBDIR / _release_dir(release_key)
    records: list[Record] = []
    bad: list[str] = []
    try:
        files = sorted(root.glob("*.json"))
    except OSError:
        return records, bad
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data["schema"] != 1 or not isinstance(data["last_seen"], str) or not data["last_seen"]:
                raise ValueError("record lacks schema 1 or a last_seen stamp")
            repos = data["repos"]
            if not isinstance(repos, dict) or not all(isinstance(v, dict) for v in repos.values()):
                raise ValueError("repos is not a mapping of mappings")
            for v in repos.values():
                sha = v.get("sha")
                if not isinstance(sha, str) or not _HEX_SHA.fullmatch(sha):
                    raise ValueError("repo sha is not hex")
                if not isinstance(v.get("dirty"), bool) or not isinstance(v.get("core"), bool):
                    raise TypeError("repo dirty/core flags are not booleans")
            records.append(
                Record(
                    release=str(data.get("release", "")),
                    repos=repos,
                    first_seen=str(data.get("first_seen", "")),
                    last_seen=str(data.get("last_seen", "")),
                    last_node=str(data.get("last_node", "")),
                    last_run_id=str(data.get("last_run_id", "")),
                    last_outcome=str(data.get("last_outcome", "")),
                    file=path.name,
                )
            )
        except OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError:
            bad.append(path.name)
    return records, bad


def forecast(
    snapshot: RepoSnapshot,
    records: list[Record],
    *,
    distance: Callable[[Path, str, str], int | None],
) -> Forecast:
    """Compare the snapshot against recorded revision sets."""
    readable = {r.name: r for r in snapshot.repos if not r.unreadable}
    dirty = tuple(r.name for r in snapshot.repos if r.dirty)
    unreadable = tuple(r.name for r in snapshot.repos if r.unreadable)
    if not records:
        return Forecast("no_records", None, (), dirty, unreadable, core_moved=False)

    current_digest = revision_set_digest(snapshot)
    exact = [r for r in records if _record_digest(r.repos) == current_digest]
    if exact:
        best = max(exact, key=lambda r: r.last_seen)
        return Forecast("exact", best, (), dirty, unreadable, core_moved=False)

    def shared(rec: Record) -> int:
        return sum(1 for n, v in rec.repos.items() if n in readable and v.get("sha") == readable[n].sha)

    best = max(records, key=lambda r: (shared(r), r.last_seen))
    diffs: list[RepoDiff] = []
    for name in sorted(set(readable) | set(best.repos)):
        cur = readable.get(name)
        rec = best.repos.get(name)
        recorded = rec.get("sha") if rec else None
        current = cur.sha if cur else None
        if recorded == current:
            continue
        core = cur.core if cur else bool(rec and rec.get("core"))
        ahead = behind = None
        if cur is not None and recorded and current:
            ahead = distance(cur.path, recorded, current)
            behind = distance(cur.path, current, recorded)
        diffs.append(RepoDiff(name, recorded, current, core, ahead, behind))
    return Forecast("nearest", best, tuple(diffs), dirty, unreadable, any(d.core for d in diffs))
