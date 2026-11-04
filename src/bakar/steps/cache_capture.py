"""Read-only capture of a hung BitBake build's kernel and lock state.

Collects what is needed to tell an NFS delegation recall, a held lock and a
spinning process apart, without root, without writing a file and without
signalling any process. Every read or command failure becomes an unreadable
item carrying its reason; nothing here raises on a missing or forbidden
source.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

_TIMEOUT_S = 10
_FORWARDED_PREFIXES = ("/downloads", "/sstate")
_LOCK_WAIT_SYMBOLS = ("locks_lock_inode_wait", "flock_lock_inode_wait", "nfs4_proc_lock", "nlmclnt_")
_SPIN_CPU_THRESHOLD = 80.0
_JOURNAL_RE = re.compile(r"nfs|lockd|lease", re.IGNORECASE)

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


@dataclass(frozen=True)
class Item:
    """One collected value: its text, or the reason it could not be read."""

    text: str | None = None
    unreadable: str | None = None

    def render(self) -> str:
        if self.unreadable is not None:
            return f"unreadable: {self.unreadable}"
        return (self.text or "").rstrip("\n")


@dataclass
class ProcCapture:
    pid: int
    ps: Item
    stack: Item
    syscall: Item
    fds: list[str] = field(default_factory=list)
    fds_unreadable: str | None = None


@dataclass
class Capture:
    procs: list[ProcCapture]
    lslocks: Item
    deleg_lease_count: Item
    nfsstat: Item
    journal: Item


@dataclass(frozen=True)
class Verdict:
    kind: str
    pid: int | None = None


def find_bitbake_pids(proc_root: Path = Path("/proc")) -> list[int]:
    """Return every process other than the caller running a bitbake program."""
    me = os.getpid()
    pids: list[int] = []
    try:
        entries = sorted(proc_root.iterdir(), key=lambda p: p.name)
    except OSError:
        return pids
    for entry in entries:
        if not entry.name.isdigit() or int(entry.name) == me:
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        argv = [a for a in raw.split(b"\0") if a][:2]
        if any(os.path.basename(a.decode(errors="replace")).startswith("bitbake") for a in argv):
            pids.append(int(entry.name))
    return pids


def _run(runner: Runner, argv: list[str]) -> Item:
    try:
        proc = runner(argv, capture_output=True, text=True, timeout=_TIMEOUT_S, check=False)
    except subprocess.TimeoutExpired:
        return Item(unreadable=f"{argv[0]} timed out after {_TIMEOUT_S}s")
    except OSError as exc:
        return Item(unreadable=f"{argv[0]}: {exc}")
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()
        suffix = f": {detail[0]}" if detail else ""
        return Item(unreadable=f"{argv[0]} exited {proc.returncode}{suffix}")
    return Item(text=proc.stdout or "")


def _read(path: Path) -> Item:
    try:
        return Item(text=path.read_text(errors="replace"))
    except OSError as exc:
        return Item(unreadable=f"{path}: {exc.strerror or exc}")


def _under(target: str, prefixes: list[str]) -> bool:
    return any(target == p or target.startswith(p.rstrip("/") + "/") for p in prefixes)


def _collect_fds(pid_dir: Path, prefixes: list[str]) -> tuple[list[str], str | None]:
    fd_dir = pid_dir / "fd"
    try:
        links = sorted(fd_dir.iterdir(), key=lambda p: p.name)
    except OSError as exc:
        return [], f"{fd_dir}: {exc.strerror or exc}"
    found: list[str] = []
    for link in links:
        try:
            target = os.readlink(link)
        except OSError:
            continue
        if _under(target, prefixes):
            found.append(f"{link.name} -> {target}")
    return found, None


def _lslocks_filtered(item: Item, prefixes: list[str]) -> Item:
    if item.unreadable is not None:
        return item
    lines = [ln for ln in (item.text or "").splitlines() if any(p in ln for p in prefixes)]
    return Item(text="\n".join(lines))


def capture(
    cache_dirs: list[Path] | list[str],
    *,
    proc_root: Path = Path("/proc"),
    runner: Runner = subprocess.run,
) -> Capture:
    prefixes = [str(d) for d in cache_dirs] + list(_FORWARDED_PREFIXES)
    procs: list[ProcCapture] = []
    for pid in find_bitbake_pids(proc_root):
        pid_dir = proc_root / str(pid)
        fds, fds_err = _collect_fds(pid_dir, prefixes)
        procs.append(
            ProcCapture(
                pid=pid,
                ps=_run(runner, ["ps", "-o", "stat=,pcpu=,wchan:32=", "-p", str(pid)]),
                stack=_read(pid_dir / "stack"),
                syscall=_read(pid_dir / "syscall"),
                fds=fds,
                fds_unreadable=fds_err,
            )
        )

    locks_raw = _read(proc_root / "locks")
    if locks_raw.unreadable is not None:
        deleg = locks_raw
    else:
        count = sum(1 for ln in (locks_raw.text or "").splitlines() if "DELEG" in ln or "LEASE" in ln)
        deleg = Item(text=str(count))

    journal = _run(runner, ["journalctl", "-k", "--since", "-1h", "--no-pager"])
    if journal.unreadable is None:
        journal = Item(text="\n".join(ln for ln in (journal.text or "").splitlines() if _JOURNAL_RE.search(ln)))

    return Capture(
        procs=procs,
        lslocks=_lslocks_filtered(_run(runner, ["lslocks", "-o", "COMMAND,PID,TYPE,MODE,PATH"]), prefixes),
        deleg_lease_count=deleg,
        nfsstat=_run(runner, ["nfsstat", "-c"]),
        journal=journal,
    )


def _ps_fields(item: Item) -> tuple[str, float | None, str]:
    """Split ``ps`` output into (state, cpu percent, wait channel)."""
    if item.unreadable is not None or not (item.text or "").strip():
        return "", None, ""
    parts = item.text.split()
    state = parts[0] if parts else ""
    try:
        cpu: float | None = float(parts[1]) if len(parts) > 1 else None
    except ValueError:
        cpu = None
    wchan = parts[2] if len(parts) > 2 else ""
    return state, cpu, wchan


def classify(cap: Capture) -> Verdict:
    """First match of delegation-recall, held-lock, spin, else unclassified."""
    for rule in ("delegation-recall", "held-lock", "spin"):
        for proc in cap.procs:
            state, cpu, wchan = _ps_fields(proc.ps)
            haystack = f"{proc.stack.text or ''} {wchan}"
            wait_symbol = wchan not in ("", "-", "0")
            if rule == "delegation-recall":
                hit = state.startswith("D") and "__break_lease" in haystack
            elif rule == "held-lock":
                hit = state.startswith("S") and any(s in haystack for s in _LOCK_WAIT_SYMBOLS)
            else:
                hit = state.startswith("R") and cpu is not None and cpu >= _SPIN_CPU_THRESHOLD and not wait_symbol
            if hit:
                return Verdict(rule, proc.pid)
    return Verdict("unclassified")


def _section(title: str, body: str) -> list[str]:
    return [f"== {title} ==", body if body else "(none)"]


def render(cap: Capture) -> str:
    lines: list[str] = []
    if not cap.procs:
        lines.append("no running BitBake process")
    for proc in cap.procs:
        lines.append(f"== process {proc.pid} ==")
        lines += ["ps:", proc.ps.render(), "stack:", proc.stack.render(), "syscall:", proc.syscall.render()]
        lines.append("fds under cache directories:")
        if proc.fds_unreadable is not None:
            lines.append(f"unreadable: {proc.fds_unreadable}")
        else:
            lines += proc.fds or ["(none)"]
    lines += _section("lslocks", cap.lslocks.render())
    lines += _section("/proc/locks DELEG/LEASE lines", cap.deleg_lease_count.render())
    lines += _section("nfsstat -c", cap.nfsstat.render())
    lines += _section("journalctl -k (nfs|lockd|lease)", cap.journal.render())
    verdict = classify(cap)
    lines.append(f"summary: {verdict.kind} pid={verdict.pid}" if verdict.pid is not None else "summary: unclassified")
    return "\n".join(lines)
