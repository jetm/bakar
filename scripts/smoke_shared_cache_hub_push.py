#!/usr/bin/env python3
"""End-to-end smoke for the shared-cache-hub-push change.

Drives this worktree's ``bakar`` (``python -m bakar`` with ``src`` on the path)
and ``bakar.steps.cache_push`` against isolated directories: a temporary HOME
and XDG tree, a scratch downloads directory, a scratch "store" directory that
stands in for the hub, PATH stubs for ``ssh``, ``rsync``, ``rrsync``,
``inotifywait`` and ``systemctl``, and a local HTTP server answering the
pusher's HEAD checks from the scratch store.

Scratch lives under /var/tmp/claude-code/bakar/, not /tmp: the build dry runs
pass bakar's 50 GB disk-free doctor gate, which a RAM-backed /tmp fails.

What needs a live host or a real BitBake parse (``bakar getvar``, the real
systemd unit, the real hub) is printed as the manual remainder.

Usage:
    uv run python scripts/smoke_shared_cache_hub_push.py
"""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import importlib.util
import json
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import urllib.parse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"
PY = sys.executable
SMOKE_LIB = Path("~/.claude/skills/ds-archive/scripts/smoke_lib.py").expanduser()
SCRATCH_PARENT = Path("/var/tmp/claude-code/bakar")
REAL_CONFIG = Path.home() / ".config" / "bakar" / "config.toml"

KAS_YAML = "header:\n  version: 14\nmachine: qemux86-64\ndistro: poky\ntarget: core-image-minimal\n"
HUB_PATTERN = "(https?|ftp|crate|gomod)://.*/.* "
UNIT = "bakar-cache-push.service"
STALL_TIMEOUT_S = 3
EXPECTED_CHECKS = 42
STRIP_ENV = ("DL_DIR", "SSTATE_DIR", "CCACHE_DIR", "SSTATE_MIRRORS", "SSTATE_MIRROR_URL", "KAS_REPO_REF_DIR")

MANUAL_STEPS_TEXT = (
    "1.2 Live eligibility on pc2 -> PC1: e2e-foo pushed with matching sha256, nothing during the 10 s wait, "
    "no directory or symlink pair in downloads-incoming. Needs the real unit and the real store.",
    "1.3 Live clash, stalled transfer (DROP rule) and blocked store (REJECT rule) across a unit restart. "
    "Needs firewall rules on PC1 and the real rsync --timeout.",
    "1.4 Live startup reconcile and a real full 1 MiB tmpfs target. Needs pc2, PC1 and a scratch ssh target.",
    "1.5 `bakar getvar PREMIRRORS`, `SSTATE_MIRRORS` and `BB_HASHSERVE_UPSTREAM` on PC1 and pc2 with and "
    "without dl_mirror_url, and the DROP-rule dry run. This smoke only checks the dry-run chain and a static "
    "simulation of the materialized PREMIRRORS prepends; the real values need a BitBake parse.",
    "1.6 `bakar cache capture` during a real build and a held flock (summary: held-lock pid=<do_fetch pid>), "
    "and the three doctor rows on the real unit, the real NFS sstate mount and PC1's downloads directory.",
    "1.7 nginx store HTTP codes (200/403/405), unit enablement on pc2/pc3/PC1, and the restricted push key "
    "refusing a shell, a command and a port forward.",
    "1.8 `bakar settings; bakar getvar DL_DIR` and the NFS mount options on pc2, pc3 and PC1, plus PC1's "
    "/etc/exports and nfsd threads. This smoke substitutes the dry run's env.DL_DIR/env.SSTATE_DIR for getvar.",
    "getvar remainder: `bakar getvar BB_FETCH_TIMEOUT` (unset) in a generic, an NXP, a TI and an "
    "example-overlay build, `bakar getvar PREMIRRORS` with no dl_mirror_url (no hub entry), and `bakar getvar "
    "DL_DIR`/`SSTATE_DIR` against the six-key config. Each needs a synced kas workspace and a BitBake parse; "
    "this smoke checks the materialized overlays statically instead.",
)


def _load_smoke_lib():
    spec = importlib.util.spec_from_file_location("smoke_lib", SMOKE_LIB)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {SMOKE_LIB}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["smoke_lib"] = module
    spec.loader.exec_module(module)
    return module


# --- process helpers ----------------------------------------------------------


@dataclass
class Result:
    rc: int | None
    out: str
    err: str
    elapsed: float

    @property
    def text(self) -> str:
        return self.out + self.err

    def describe(self) -> str:
        return f"rc={self.rc} out={self.out.strip()[-400:]!r} err={self.err.strip()[-400:]!r}"


def _stop_group(child: subprocess.Popen, first: int = signal.SIGTERM, grace: float = 5.0) -> None:
    """Signal only the child's own process group; refuse when it has none."""
    if child.poll() is not None:
        return
    try:
        pgid = os.getpgid(child.pid)
    except ProcessLookupError:
        return
    if pgid != child.pid:
        print(f"WARNING: pid {child.pid} has no own process group; not signalling it")
        child.wait()
        return
    for sig in (first, signal.SIGTERM, signal.SIGKILL):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pgid, sig)
        try:
            child.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            continue
        return


def run_cmd(argv: list[str], env: dict[str, str], cwd: Path, timeout: float = 180) -> Result:
    with tempfile.TemporaryFile("w+") as out, tempfile.TemporaryFile("w+") as err:
        t0 = time.monotonic()
        child = subprocess.Popen(
            argv, env=env, cwd=cwd, stdout=out, stderr=err, stdin=subprocess.DEVNULL, start_new_session=True
        )
        try:
            rc: int | None = child.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _stop_group(child)
            rc = None
        elapsed = time.monotonic() - t0
        out.seek(0)
        err.seek(0)
        return Result(rc, out.read(), err.read(), elapsed)


def bakar(box: Box, *args: str, env: dict[str, str] | None = None, timeout: float = 180) -> Result:
    return run_cmd([PY, "-m", "bakar", *args], env or box.env, box.ws, timeout)


def wait_for(pred, timeout: float, step: float = 0.25) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(step)
    return bool(pred())


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- stubs ----------------------------------------------------------------------

RSYNC_STUB = """#!{py}
# rsync stand-in: copies the --files-from names from the source directory into the
# local path named after the target's "host:" prefix. Mode comes from rsync.mode.
import json, os, shutil, sys, time
from pathlib import Path
here = Path(__file__).resolve().parent
args = sys.argv[1:]
mode_file = here / "rsync.mode"
mode = mode_file.read_text().strip() if mode_file.exists() else "ok"
pos, files_from, timeout, i = [], None, None, 0
while i < len(args):
    a = args[i]
    if a == "-e":
        i += 2
        continue
    if a.startswith("--files-from="):
        files_from = a.split("=", 1)[1]
    elif a.startswith("--timeout="):
        timeout = int(a.split("=", 1)[1])
    elif not a.startswith("-"):
        pos.append(a)
    i += 1
names = []
if files_from:
    names = [n.decode() for n in Path(files_from).read_bytes().split(b"\\0") if n]
with open(here / "rsync.log", "a") as fh:
    fh.write(json.dumps({{"argv": args, "mode": mode, "names": names, "t": time.time()}}) + "\\n")
if mode == "fail":
    print("ssh: connect to host hub.invalid port 22: Connection refused", file=sys.stderr)
    print("rsync: connection unexpectedly closed (0 bytes received so far) [sender]", file=sys.stderr)
    sys.exit(255)
if mode == "full":
    print('rsync: [receiver] write failed on "x": No space left on device (28)', file=sys.stderr)
    sys.exit(11)
if mode == "sleep":
    # A stalled peer: no data ever moves, so rsync's own --timeout ends the run.
    time.sleep(timeout if timeout else 3600)
    print(f"[sender] io timeout after {{timeout}} seconds -- exiting", file=sys.stderr)
    print("rsync error: timeout in data send/receive (code 30)", file=sys.stderr)
    sys.exit(30)
src, dst = pos[-2], pos[-1]
store = Path(dst.split(":", 1)[1] if ":" in dst else dst)
ignore = "--ignore-existing" in args
dry = "--dry-run" in args
for n in names:
    s, d = Path(src) / n, store / n
    if ignore and (d.exists() or d.is_symlink()):
        continue
    if dry:
        print(n)
        continue
    if s.is_symlink():
        os.symlink(os.readlink(s), d)
    elif s.is_dir():
        shutil.copytree(s, d, symlinks=True)
    else:
        shutil.copy2(s, d)
sys.exit(0)
"""

INOTIFY_STUB = """#!{py}
# inotifywait -m stand-in: polls the watched directory and prints the name of every
# entry that appears or changes. Exits when its parent goes away.
import os, sys, time
d = sys.argv[-1]
ppid = os.getppid()
def snap():
    out = {{}}
    for e in os.scandir(d):
        try:
            st = e.stat(follow_symlinks=False)
        except OSError:
            continue
        out[e.name] = (st.st_mtime_ns, st.st_size)
    return out
prev = snap()
while os.getppid() == ppid:
    time.sleep(0.1)
    cur = snap()
    for name, sig in sorted(cur.items()):
        if prev.get(name) != sig:
            try:
                print(name, flush=True)
            except BrokenPipeError:
                sys.exit(0)
    prev = cur
"""

SSH_STUB = """#!/usr/bin/env bash
# The pusher reaches ssh only through rsync -e; this stub records any direct call.
printf '%s\\n' "$*" >> "$(dirname "$0")/ssh.log"
echo "ssh stub: no network in the smoke" >&2
exit 255
"""

RRSYNC_STUB = """#!/usr/bin/env bash
echo "rrsync stub: runs only on the hub side" >&2
exit 1
"""

SYSTEMCTL_STUB = """#!/usr/bin/env bash
mode=$(cat "$(dirname "$0")/systemctl.mode" 2>/dev/null || echo inactive)
if [ "${1:-}" = "--user" ] && [ "${2:-}" = "is-active" ]; then
  echo "$mode"
  [ "$mode" = active ] && exit 0 || exit 3
fi
exit 0
"""


class Stubs:
    def __init__(self, root: Path) -> None:
        self.dir = root / "stubs"
        self.dir.mkdir()
        for name, body in (
            ("rsync", RSYNC_STUB.format(py=PY)),
            ("inotifywait", INOTIFY_STUB.format(py=PY)),
            ("ssh", SSH_STUB),
            ("rrsync", RRSYNC_STUB),
            ("systemctl", SYSTEMCTL_STUB),
        ):
            path = self.dir / name
            path.write_text(body)
            path.chmod(0o755)
        self.rsync("ok")
        self.systemctl("inactive")

    def rsync(self, mode: str) -> None:
        (self.dir / "rsync.mode").write_text(mode)

    def systemctl(self, mode: str) -> None:
        (self.dir / "systemctl.mode").write_text(mode)

    def rsync_calls(self) -> list[dict]:
        log = self.dir / "rsync.log"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


# --- local servers ------------------------------------------------------------------


class Hub:
    """HTTP stand-in for the hub's nginx store: 200 for a regular file under root, else 404."""

    def __init__(self) -> None:
        self.root: Path | None = None
        self.requests: list[tuple[str, str]] = []
        hub = self

        class Handler(BaseHTTPRequestHandler):
            def _answer(self) -> None:
                path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
                hub.requests.append((self.command, path))
                ok = False
                name = path.removeprefix("/downloads/") if path.startswith("/downloads/") else ""
                if name and "/" not in name and hub.root is not None:
                    try:
                        ok = stat.S_ISREG(os.lstat(hub.root / name).st_mode)
                    except OSError:
                        ok = False
                self.send_response(200 if ok else 404)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_HEAD(self) -> None:
                self._answer()

            def do_GET(self) -> None:
                self._answer()

            def log_message(self, *_a) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/downloads"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class Listener:
    """A bare TCP listener that counts accepted connections."""

    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.accepted = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except TimeoutError, OSError:
                continue
            self.accepted += 1
            conn.close()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        self.sock.close()


def closed_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --- isolated boxes ------------------------------------------------------------------


def _real_buildtools_dirs() -> dict[str, str]:
    """The host's buildtools table, read-only, so the dry-run doctor gate finds a toolchain."""
    try:
        data = tomllib.loads(REAL_CONFIG.read_text())
    except OSError, tomllib.TOMLDecodeError:
        return {}
    table = data.get("build", {}).get("buildtools_dirs", {})
    return {k: v for k, v in table.items() if isinstance(v, str)}


BUILDTOOLS = _real_buildtools_dirs()
# A fresh workspace has no release key yet, so the per-release table alone is not
# consulted; the env override is what the dry-run doctor gate accepts.
BUILDTOOLS_DIR = BUILDTOOLS.get("scarthgap") or next(iter(BUILDTOOLS.values()), "")


@dataclass
class Box:
    root: Path
    stubs: Stubs
    home: Path = field(init=False)
    state: Path = field(init=False)
    dl: Path = field(init=False)
    sstate: Path = field(init=False)
    store: Path = field(init=False)
    ws: Path = field(init=False)
    env: dict[str, str] = field(init=False)

    def __post_init__(self) -> None:
        self.home = self.root / "home"
        self.state = self.root / "state"
        self.dl = self.root / "downloads"
        self.sstate = self.root / "sstate"
        self.store = self.root / "store"
        self.ws = self.root / "ws"
        for d in (self.home / ".config" / "bakar", self.home / ".ssh", self.state, self.dl, self.sstate):
            d.mkdir(parents=True)
        self.store.mkdir()
        self.ws.mkdir()
        (self.ws / "kas.yml").write_text(KAS_YAML)
        (self.ws / ".bakar.toml").write_text("")
        (self.home / ".gitconfig").write_text("[user]\n\temail = smoke@example.com\n\tname = Smoke\n")
        # A dummy key for the prerequisite check; nothing ever reads it.
        (self.home / ".ssh" / "bakar-cache-push").write_text("dummy smoke key, not a real key\n")
        (self.home / ".ssh" / "bakar-cache-push").chmod(0o600)
        env = {k: v for k, v in os.environ.items() if not k.startswith("BAKAR_") and k not in STRIP_ENV}
        env.update(
            {
                "HOME": str(self.home),
                "XDG_STATE_HOME": str(self.state),
                "XDG_CONFIG_HOME": str(self.home / ".config"),
                "XDG_DATA_HOME": str(self.root / "data"),
                "XDG_CACHE_HOME": str(self.root / "cache"),
                "PYTHONPATH": str(SRC),
                "NO_COLOR": "1",
                "TERM": "dumb",
                "COLUMNS": "200",
                "PATH": f"{self.stubs.dir}:{os.environ.get('PATH', '/usr/bin')}",
            }
        )
        if BUILDTOOLS_DIR:
            env["BAKAR_BUILDTOOLS_DIR"] = BUILDTOOLS_DIR
        self.env = env

    @property
    def target(self) -> str:
        return f"smokehub:{self.store}"

    @property
    def key(self) -> Path:
        return self.home / ".ssh" / "bakar-cache-push"

    @property
    def queue_dir(self) -> Path:
        return self.state / "bakar" / "cache-push"

    def configure(self, **build: str) -> None:
        lines = ["[build]"]
        lines += [f"{k} = {json.dumps(v)}" for k, v in build.items()]
        if BUILDTOOLS:
            lines.append("")
            lines.append("[build.buildtools_dirs]")
            lines += [f"{k} = {json.dumps(v)}" for k, v in BUILDTOOLS.items()]
        (self.home / ".config" / "bakar" / "config.toml").write_text("\n".join(lines) + "\n")

    def base(self, **extra: str) -> dict[str, str]:
        return {"dl_dir": str(self.dl), "sstate_dir": str(self.sstate), **extra}

    def store_names(self) -> set[str]:
        return {p.name for p in self.store.iterdir()}


def new_box(root: Path, name: str, stubs: Stubs) -> Box:
    path = root / name
    path.mkdir()
    return Box(path, stubs)


def status(box: Box) -> tuple[Result, dict[str, str]]:
    res = bakar(box, "cache", "status", timeout=60)
    fields: dict[str, str] = {}
    for line in res.out.splitlines():
        key, sep, value = line.partition(": ")
        if sep:
            fields[key.strip()] = value.strip()
    return res, fields


class Watcher:
    """`bakar cache push --watch` in its own session; stopped with SIGINT to its group."""

    def __init__(self, box: Box) -> None:
        self.log = box.root / "watch.log"
        self.started = time.time()
        with self.log.open("w") as fh:
            self.proc = subprocess.Popen(
                [PY, "-m", "bakar", "cache", "push", "--watch"],
                env=box.env,
                cwd=box.ws,
                stdout=fh,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )

    def stop(self) -> str:
        _stop_group(self.proc, first=signal.SIGINT, grace=10)
        return self.log.read_text(errors="replace")


@contextlib.contextmanager
def watching(box: Box):
    w = Watcher(box)
    try:
        yield w
    finally:
        w.stop()


def write_pair(dl: Path, name: str, payload: bytes, *, stamp: bool = True) -> None:
    (dl / name).write_bytes(payload)
    if stamp:
        (dl / f"{name}.done").write_bytes(b"")


def queue_len(box: Box) -> str | None:
    return status(box)[1].get("queue")


# --- in-process seams ------------------------------------------------------------------


def import_bakar():
    for p in (str(SRC), str(REPO)):
        if p not in sys.path:
            sys.path.insert(0, p)
    from bakar.steps import cache_push, cache_queue

    return cache_push, cache_queue


# --- scenarios ---------------------------------------------------------------------------


def scenario_a(smoke, root: Path, stubs: Stubs, hub: Hub) -> None:
    box = new_box(root, "a", stubs)
    box.configure(**box.base(cache_push_target=box.target, dl_mirror_url=hub.url))
    hub.root = box.store
    stubs.rsync("ok")
    write_pair(box.dl, "e2e-foo-1.0.tar.gz", os.urandom(256 * 1024))
    (box.dl / "e2e-dir-1.0").mkdir()
    (box.dl / "e2e-dir-1.0" / "inner").write_text("x")
    (box.dl / "e2e-dir-1.0.done").write_bytes(b"")
    (box.dl / "e2e-link-1.0.tar.gz").symlink_to(box.dl / "e2e-foo-1.0.tar.gz")
    (box.dl / "e2e-link-1.0.tar.gz.done").write_bytes(b"")
    write_pair(box.dl, "git2_e2e.example.com.repo.git.tar.gz", b"git2")
    write_pair(box.dl, "gitshallow_e2e.example.com.repo.git.tar.gz", b"shallow")
    with watching(box) as w:
        arrived = wait_for((box.store / "e2e-foo-1.0.tar.gz.done").exists, 30)
        # A payload whose stamp has not been written yet must stay local.
        (box.dl / "e2e-nostamp-1.0.tar.gz").write_bytes(b"in flight")
        time.sleep(5)
        names = box.store_names()
    log = w.log.read_text(errors="replace")
    want = {"e2e-foo-1.0.tar.gz", "e2e-foo-1.0.tar.gz.done"}
    smoke.check(
        "(a) store receives exactly e2e-foo-1.0.tar.gz and its .done",
        arrived and (names - {"e2e-nostamp-1.0.tar.gz"}) == want,
        f"arrived={arrived} store={sorted(names)} watch log={log[-400:]!r}",
    )
    foo = box.store / "e2e-foo-1.0.tar.gz"
    smoke.check(
        "(a) pushed payload is a regular file with the source sha256",
        foo.exists() and stat.S_ISREG(os.lstat(foo).st_mode) and sha256(foo) == sha256(box.dl / "e2e-foo-1.0.tar.gz"),
        f"exists={foo.exists()}",
    )
    bad = [
        p.name
        for p in box.store.iterdir()
        if not stat.S_ISREG(os.lstat(p).st_mode) or p.name.startswith(("git2_", "gitshallow_", "e2e-dir-", "e2e-link-"))
    ]
    smoke.check("(a) no directory, symlink or git-mirror name (or stamp) in the store", not bad, f"bad={bad}")
    smoke.check(
        "(a) a payload with no .done yet is not pushed",
        "e2e-nostamp-1.0.tar.gz" not in names,
        f"store={sorted(names)}",
    )


def scenario_b(smoke, root: Path, stubs: Stubs, hub: Hub) -> None:
    box = new_box(root, "b", stubs)
    box.configure(**box.base(cache_push_target=box.target, dl_mirror_url=hub.url))
    hub.root = box.store
    stubs.rsync("ok")
    # clash: the hub already serves payload and stamp; clash2: payload only.
    (box.store / "e2e-clash-1.0.tar.gz").write_text("original")
    (box.store / "e2e-clash-1.0.tar.gz.done").write_bytes(b"")
    (box.store / "e2e-clash2-1.0.tar.gz").write_text("original")
    calls_before = len(stubs.rsync_calls())
    hub.requests.clear()
    with watching(box) as w:
        time.sleep(1.5)
        write_pair(box.dl, "e2e-clash-1.0.tar.gz", b"different")
        write_pair(box.dl, "e2e-clash2-1.0.tar.gz", b"different")
        drained = wait_for((box.store / "e2e-clash2-1.0.tar.gz.done").exists, 30)
        wait_for(lambda: queue_len(box) == "0", 20, step=0.5)
    log = w.log.read_text(errors="replace")
    clash = (box.store / "e2e-clash-1.0.tar.gz").read_text()
    clash2 = (box.store / "e2e-clash2-1.0.tar.gz").read_text()
    smoke.check(
        "(b) store clash files still read `original`",
        clash == "original" and clash2 == "original" and drained,
        f"clash={clash!r} clash2={clash2!r} drained={drained} log={log[-300:]!r}",
    )
    heads = {p for (m, p) in hub.requests if m == "HEAD"}
    pushes = [c for c in stubs.rsync_calls()[calls_before:] if "--dry-run" not in c["argv"]]
    pushed_clash = [c for c in pushes if "e2e-clash-1.0.tar.gz" in c["names"]]
    smoke.check(
        "(b) pusher HEADs payload AND stamp and does not transfer the pair the hub serves",
        {"/downloads/e2e-clash-1.0.tar.gz", "/downloads/e2e-clash-1.0.tar.gz.done"} <= heads and not pushed_clash,
        f"heads={sorted(heads)} pushes naming clash={pushed_clash}",
    )
    clash2_pushes = [c for c in pushes if "e2e-clash2-1.0.tar.gz" in c["names"]]
    smoke.check(
        "(b) a payload-only clash is pushed with --ignore-existing and never overwritten",
        bool(clash2_pushes) and all("--ignore-existing" in c["argv"] for c in clash2_pushes),
        f"clash2 pushes={clash2_pushes}",
    )


def scenario_c(smoke, root: Path, stubs: Stubs, hub: Hub) -> None:
    cache_push, cache_queue = import_bakar()
    box = new_box(root, "c", stubs)
    box.configure(**box.base(cache_push_target=box.target, dl_mirror_url=hub.url))
    hub.root = box.store
    stubs.rsync("fail")
    with watching(box) as w:
        time.sleep(1.0)
        write_pair(box.dl, "e2e-q1-1.0.tar.gz", os.urandom(4096))
        write_pair(box.dl, "e2e-q2-1.0.tar.gz", os.urandom(4096))
        wait_for(lambda: queue_len(box) == "4", 20, step=0.5)
        time.sleep(2.5)  # past the 2 s settle, so at least one push has failed
        _, s1 = status(box)
        time.sleep(1.0)
        _, s2 = status(box)
        stable = (
            s1.get("queue") == s2.get("queue")
            and s1.get("queue") not in (None, "0")
            and s1.get("oldest", "").isdigit()
            and s2.get("oldest", "").isdigit()
        )
        smoke.check(
            "(c) failing transfer: status shows the same non-zero queue and numeric oldest twice",
            stable,
            f"s1={s1} s2={s2}",
        )

        # The pusher call itself returns promptly on a failed transfer (separate queue).
        side = new_box(root, "c-side", stubs)
        write_pair(side.dl, "e2e-side-1.0.tar.gz", b"side")
        with cache_queue.PushQueue(side.queue_dir) as q:
            q.enqueue(["e2e-side-1.0.tar.gz", "e2e-side-1.0.tar.gz.done"])
            t0 = time.monotonic()
            outcome = cache_push.drain_once(q, side.dl, side.target, side.key)
            elapsed = time.monotonic() - t0
            left = q.pending()
        smoke.check(
            "(c) drain_once returns promptly on a failed transfer and keeps the entries",
            elapsed < 5 and outcome is not cache_push.Outcome.OK and len(left) == 2,
            f"elapsed={elapsed:.2f}s outcome={outcome} pending={left}",
        )

        stubs.rsync("ok")
        arrived = wait_for(
            lambda: all((box.store / n).exists() for n in ("e2e-q1-1.0.tar.gz.done", "e2e-q2-1.0.tar.gz.done")), 60
        )
        wait_for(lambda: queue_len(box) == "0", 20, step=0.5)
        _, s3 = status(box)
    log = w.log.read_text(errors="replace")
    same = all(
        (box.store / n).is_file() and sha256(box.store / n) == sha256(box.dl / n)
        for n in ("e2e-q1-1.0.tar.gz", "e2e-q2-1.0.tar.gz", "e2e-q1-1.0.tar.gz.done", "e2e-q2-1.0.tar.gz.done")
    )
    smoke.check(
        "(c) after the stub succeeds both payloads and both stamps reach the store unattended",
        arrived and same,
        f"arrived={arrived} store={sorted(box.store_names())} log={log[-300:]!r}",
    )
    smoke.check(
        "(c) status then prints queue: 0 and oldest: -",
        s3.get("queue") == "0" and s3.get("oldest") == "-",
        f"status={s3}",
    )


def scenario_d(smoke, root: Path, stubs: Stubs) -> None:
    cache_push, cache_queue = import_bakar()
    from bakar.steps.remote_dispatch import RSYNC_IO_TIMEOUT_SECONDS

    box = new_box(root, "d", stubs)
    argv = cache_push.build_push_argv(box.dl, box.target, box.key, box.root / "list")
    smoke.check(
        "(d) push argv carries the configured rsync I/O timeout",
        f"--timeout={RSYNC_IO_TIMEOUT_SECONDS}" in argv
        and cache_push.RSYNC_IO_TIMEOUT_SECONDS == RSYNC_IO_TIMEOUT_SECONDS,
        f"argv={argv}",
    )
    write_pair(box.dl, "e2e-stall-1.0.tar.gz", b"stall")
    stubs.rsync("sleep")
    calls_before = len(stubs.rsync_calls())
    original = cache_push.RSYNC_IO_TIMEOUT_SECONDS
    cache_push.RSYNC_IO_TIMEOUT_SECONDS = STALL_TIMEOUT_S
    try:
        with cache_queue.PushQueue(box.queue_dir) as q:
            q.enqueue(["e2e-stall-1.0.tar.gz", "e2e-stall-1.0.tar.gz.done"])
            t0 = time.monotonic()
            outcome = cache_push.drain_once(q, box.dl, box.target, box.key)
            elapsed = time.monotonic() - t0
            left = q.pending()
    finally:
        cache_push.RSYNC_IO_TIMEOUT_SECONDS = original
        stubs.rsync("ok")
    calls = stubs.rsync_calls()[calls_before:]
    smoke.check(
        f"(d) stalled rsync ends non-zero within the I/O timeout ({STALL_TIMEOUT_S}s here) and the entry stays queued",
        outcome is not cache_push.Outcome.OK
        and elapsed <= STALL_TIMEOUT_S + 2
        and len(left) == 2
        and any(f"--timeout={STALL_TIMEOUT_S}" in c["argv"] for c in calls),
        f"outcome={outcome} elapsed={elapsed:.2f}s pending={left} calls={calls}",
    )


def scenario_e(smoke, root: Path, stubs: Stubs, hub: Hub) -> None:
    box = new_box(root, "e", stubs)
    box.configure(**box.base(cache_push_target=box.target, dl_mirror_url=hub.url))
    hub.root = box.store
    stubs.rsync("ok")
    write_pair(box.dl, "e2e-recon-1.0.tar.gz", os.urandom(8192))
    started = time.time()
    with watching(box):
        arrived = wait_for(
            lambda: (
                (box.store / "e2e-recon-1.0.tar.gz").exists() and (box.store / "e2e-recon-1.0.tar.gz.done").exists()
            ),
            30,
        )
        wait_for(lambda: queue_len(box) == "0", 20, step=0.5)
        _, st = status(box)
    smoke.check(
        "(e) startup reconcile pushes a pair created while no watcher ran",
        arrived and sha256(box.store / "e2e-recon-1.0.tar.gz") == sha256(box.dl / "e2e-recon-1.0.tar.gz"),
        f"store={sorted(box.store_names())}",
    )
    try:
        reconciled = datetime.datetime.fromisoformat(st.get("last reconcile", "")).timestamp()
    except ValueError:
        reconciled = None
    smoke.check(
        "(e) status prints queue: 0 and a last reconcile time after the start",
        st.get("queue") == "0" and reconciled is not None and reconciled >= started - 1,
        f"status={st} started={started}",
    )


def scenario_f(smoke, root: Path, stubs: Stubs) -> None:
    cache_push, cache_queue = import_bakar()
    box = new_box(root, "f", stubs)
    box.configure(**box.base(cache_push_target=box.target))
    write_pair(box.dl, "e2e-full-1.0.tar.gz", b"full")
    stubs.rsync("full")
    try:
        with cache_queue.PushQueue(box.queue_dir) as q:
            q.enqueue(["e2e-full-1.0.tar.gz", "e2e-full-1.0.tar.gz.done"])
            first = cache_push.drain_once(q, box.dl, box.target, box.key)
            _, s1 = status(box)
            stubs.rsync("ok")
            second = cache_push.drain_once(q, box.dl, box.target, box.key)
            _, s2 = status(box)
    finally:
        stubs.rsync("ok")
    paused = s1.get("paused", "")
    reason = paused.removeprefix("yes (").removesuffix(")").lower()
    smoke.check(
        "(f) full target: status shows paused: yes (<disk full / no space left>) with the entry still queued",
        first is cache_push.Outcome.FULL
        and paused.startswith("yes (")
        and ("no space left" in reason or "disk full" in reason)
        and s1.get("queue") == "2",
        f"outcome={first} status={s1}",
    )
    smoke.check(
        "(f) after space returns: paused: no, queue: 0, files in the store",
        second is cache_push.Outcome.OK
        and s2.get("paused") == "no"
        and s2.get("queue") == "0"
        and {"e2e-full-1.0.tar.gz", "e2e-full-1.0.tar.gz.done"} <= box.store_names(),
        f"outcome={second} status={s2} store={sorted(box.store_names())}",
    )


def scenario_g(smoke, root: Path, stubs: Stubs) -> None:
    box = new_box(root, "g", stubs)
    box.configure(**box.base())
    res, st = status(box)
    lr = st.get("last reconcile", "")
    ok_time = lr == "never"
    if not ok_time:
        with contextlib.suppress(ValueError):
            datetime.datetime.fromisoformat(lr)
            ok_time = True
    smoke.check(
        "(g) empty queue: queue: 0, oldest: -, paused: no, last reconcile: <time|never>, exit 0",
        res.rc == 0 and st.get("queue") == "0" and st.get("oldest") == "-" and st.get("paused") == "no" and ok_time,
        res.describe(),
    )


def scenario_h(smoke, root: Path, stubs: Stubs) -> None:
    calls_before = len(stubs.rsync_calls())
    box = new_box(root, "h1", stubs)
    box.configure(**box.base())
    res = bakar(box, "cache", "push", "--watch", timeout=20)
    smoke.check(
        "(h) push --watch without cache_push_target exits non-zero at once naming it, enqueues nothing",
        res.rc not in (0, None)
        and "cache_push_target" in res.text
        and res.elapsed < 10
        and not (box.queue_dir / "queue.db").exists()
        and len(stubs.rsync_calls()) == calls_before,
        f"{res.describe()} elapsed={res.elapsed:.1f}s",
    )
    box2 = new_box(root, "h2", stubs)
    box2.configure(**box2.base(cache_push_target=box2.target))
    write_pair(box2.dl, "e2e-h-1.0.tar.gz", b"h")
    env = {**box2.env, "PATH": "/var/empty"}
    res2 = bakar(box2, "cache", "push", "--watch", env=env, timeout=20)
    smoke.check(
        "(h) push --watch with PATH=/var/empty exits non-zero at once naming inotifywait, transfers nothing",
        res2.rc not in (0, None)
        and "inotifywait" in res2.text
        and res2.elapsed < 10
        and not (box2.queue_dir / "queue.db").exists()
        and len(stubs.rsync_calls()) == calls_before
        and not box2.store_names(),
        f"{res2.describe()} elapsed={res2.elapsed:.1f}s",
    )


def _chain(res: Result) -> list[str]:
    for line in res.text.splitlines():
        if line.startswith("overlay: "):
            return [Path(p).name for p in line.removeprefix("overlay: ").strip().split(":")]
    return []


def _env_lines(res: Result) -> dict[str, str]:
    out = {}
    for line in res.text.splitlines():
        if line.startswith("env."):
            key, _, value = line.partition(": ")
            out[key.removeprefix("env.")] = value
    return out


def _materialize(root: Path, family: str, names: list[str]) -> list[Path]:
    import_bakar()
    import bakar
    from bakar.steps.kas_overlay import materialize_overlay
    from tests.conftest import make_build_config

    ws = root / f"mat-{family}"
    ws.mkdir(exist_ok=True)
    cfg = make_build_config(workspace=ws, bsp_family=family)
    src = Path(bakar.__file__).parent / "overlays"
    return [cfg.bsp_root / materialize_overlay(cfg, src / n, is_main_overlay=i == 0) for i, n in enumerate(names)]


_PREMIRRORS_RE = re.compile(r'^\s*PREMIRRORS(?P<op>:prepend|:append)?\s*(?P<eq>\?\?=|\?=|:=|=)\s*"(?P<val>.*)"\s*$')


def _premirrors_ops(overlays: list[Path]) -> list[tuple[str, str]]:
    """PREMIRRORS operations in local.conf order: kas merges local_conf_header and emits it key-sorted."""
    import yaml

    merged: dict[str, str] = {}
    for path in overlays:
        merged.update((yaml.safe_load(path.read_text()) or {}).get("local_conf_header") or {})
    ops = []
    for key in sorted(merged):
        joined = re.sub(r"\\\n", " ", merged[key])
        for line in joined.splitlines():
            m = _PREMIRRORS_RE.match(line)
            if m:
                ops.append((m.group("op") or m.group("eq"), m.group("val")))
    return ops


def _expand(value: str, datastore: dict[str, str]) -> str:
    """Evaluate an inline-python value as a task sees it.

    bitbake-worker rebuilds os.environ from the exported datastore variables only, so a
    passthrough variable is visible through ``d.getVar`` and absent from ``os.environ``.
    """
    if value.startswith("${@") and value.endswith("}"):
        fake_os = type("FakeOs", (), {"environ": {}})
        fake_d = type("FakeDatastore", (), {"getVar": staticmethod(datastore.get)})
        return str(eval(value[3:-1], {"os": fake_os, "d": fake_d}))
    return value


def scenario_i(smoke, root: Path, stubs: Stubs, hub: Hub) -> None:
    box = new_box(root, "i", stubs)
    listener = Listener()
    try:
        box.configure(**box.base())
        r1 = bakar(box, "--hide-doctor-report", "build", "--dry-run", "kas.yml")
        smoke.check(
            "(i) no dl_mirror_url: dry run exits 0, no mirror warning, no hub-cache overlay, no hub connect",
            r1.rc == 0
            and "hub mirror" not in r1.text
            and "hub-cache" not in r1.text
            and bool(_chain(r1))
            and listener.accepted == 0,
            f"{r1.describe()} accepted={listener.accepted}",
        )
    finally:
        listener.close()

    port = closed_port()
    dead = f"http://127.0.0.1:{port}/downloads"
    box.configure(**box.base(dl_mirror_url=dead))
    r2 = bakar(box, "--hide-doctor-report", "build", "--dry-run", "kas.yml")
    warnings = [line for line in r2.text.splitlines() if "warning" in line.lower()]
    smoke.check(
        "(i) unreachable dl_mirror_url: one warning naming the URL, <= ~3 s extra, no hub-cache overlay, exit 0",
        r2.rc == 0
        and len(warnings) == 1
        and dead in warnings[0]
        and "hub-cache" not in " ".join(_chain(r2))
        and r2.elapsed - r1.elapsed <= 3.5,
        f"warnings={warnings} elapsed={r2.elapsed:.1f}s base={r1.elapsed:.1f}s {r2.describe()}",
    )

    hub.root = box.store
    hub.requests.clear()
    box.configure(**box.base(dl_mirror_url=hub.url))
    r3 = bakar(box, "--hide-doctor-report", "build", "--dry-run", "kas.yml")
    chain1, chain3 = _chain(r1), _chain(r3)
    smoke.check(
        "(i) answering hub: dry run probes it, adds bakar-tuning-hub-cache.yml to the chain and exits 0",
        r3.rc == 0 and "bakar-tuning-hub-cache.yml" in chain3 and bool(hub.requests),
        f"chain={chain3} hub requests={hub.requests[:3]} {r3.describe()}",
    )
    env1 = {k: v for k, v in _env_lines(r1).items() if k != "BAKAR_DL_MIRROR_URL"}
    env3 = {k: v for k, v in _env_lines(r3).items() if k != "BAKAR_DL_MIRROR_URL"}
    smoke.check(
        "(i) hub mirror changes nothing else: env and chain equal the hub-less run apart from the hub overlay",
        env1 == env3 and bool(env1) and [n for n in chain3 if n != "bakar-tuning-hub-cache.yml"] == chain1,
        f"env diff={set(env1.items()) ^ set(env3.items())} chain1={chain1} chain3={chain3}",
    )

    # Static stand-in for `bakar getvar PREMIRRORS`: apply the materialized overlays'
    # PREMIRRORS operations in local.conf order with BitBake's :prepend semantics.
    mapping = {n: n for n in chain3 if n.startswith("bakar-tuning-") and n != "bakar-tuning-host.yml"}
    overlays = _materialize(root, "generic", [n for n in chain3 if n in mapping])
    value = ""
    problems = []
    for op, raw in _premirrors_ops(overlays):
        expanded = _expand(raw, {"BAKAR_DL_MIRROR_URL": hub.url})
        if op == ":prepend":
            value = expanded + value
        elif op == ":append":
            value += expanded
        else:
            problems.append(f"plain assignment {op} {raw[:60]!r}")
    entries = [e.strip() for e in value.split("\\n") if e.strip()]
    first = entries[0] if entries else ""
    hub_entries = [e for e in entries if hub.url in e]
    smoke.check(
        "(i) static PREMIRRORS: hub entry `(https?|ftp|crate|gomod)://.*/.* <url>/` first, git/gitsm unmapped",
        not problems
        and first == f"{HUB_PATTERN}{hub.url}/"
        and len(hub_entries) == 1
        and not re.search(r"\bgit(sm)?\b", hub_entries[0].split()[0]),
        f"problems={problems} first={first!r} hub entries={hub_entries} overlays={[p.name for p in overlays]}",
    )


def scenario_j(smoke, root: Path, stubs: Stubs) -> None:
    box = new_box(root, "j", stubs)
    box.configure(**box.base())
    if shutil.which("unshare") is None:
        smoke.check(
            "(j) capture with no BitBake process", ok=False, detail="unshare not installed; cannot isolate /proc"
        )
        smoke.check("(j) capture with a stub bitbake process tree", ok=False, detail="unshare not installed")
        return
    # A private PID namespace gives the capture a /proc with no real BitBake in it.
    ns = ["unshare", "-rpf", "--mount-proc", "bash", "-c"]
    r1 = run_cmd([*ns, '"$0" -m bakar cache capture; echo "rc=$?"', PY], box.env, box.ws, 60)
    lines1 = [line for line in r1.out.splitlines() if line.strip()]
    smoke.check(
        "(j) capture with no BitBake process says so and exits 0",
        r1.rc == 0 and "no running BitBake process" in r1.out and lines1[-1:] == ["rc=0"],
        r1.describe(),
    )
    script = '(exec -a bitbake-server sleep 60) & sleep 0.5; "$0" -m bakar cache capture; echo "rc=$?"'
    r2 = run_cmd([*ns, script, PY], box.env, box.ws, 60)
    lines2 = [line for line in r2.out.splitlines() if line.strip()]
    unreadable = [line for line in lines2 if line.startswith("unreadable:")]
    smoke.check(
        "(j) stub bitbake tree: a section per process, each unreadable item has a reason, ends with summary:",
        r2.rc == 0
        and sum(1 for line in lines2 if line.startswith("== process ")) >= 1
        and all(line.removeprefix("unreadable:").strip() for line in unreadable)
        and len(lines2) >= 2
        and lines2[-2].startswith("summary:")
        and lines2[-1] == "rc=0",
        f"unreadable={unreadable[:4]} {r2.describe()}",
    )


def _doctor(box: Box, *, nfs: bool | None = None) -> tuple[Result, dict[str, dict]]:
    if nfs is None:
        res = bakar(box, "doctor", "--json", "kas.yml")
    else:
        # The unit tests' seam: replace bakar.mounts.is_path_on_nfs, then run the real CLI.
        code = (
            "import sys; from bakar import mounts; mounts.is_path_on_nfs = lambda p: "
            f"{nfs!r}; from bakar.cli import main; sys.argv = ['bakar', 'doctor', '--json', 'kas.yml']; "
            "sys.exit(main())"
        )
        res = run_cmd([PY, "-c", code], box.env, box.ws)
    try:
        doc = json.loads(res.out)
        rows = {f["check"]: f for f in doc.get("findings", [])}
    except ValueError, AttributeError:
        rows = {}
    return res, rows


def _row(rows: dict[str, dict], name: str) -> str:
    r = rows.get(name)
    return "missing" if r is None else f"{r['status']}/{r['severity']}: {r['message']}"


def scenario_k(smoke, root: Path, stubs: Stubs) -> None:
    _, cache_queue = import_bakar()
    box = new_box(root, "k", stubs)
    box.configure(**box.base(cache_push_target=box.target))
    row = "cache-push-service"

    stubs.systemctl("inactive")
    res, rows = _doctor(box)
    r = rows.get(row, {})
    smoke.check(
        "(k) cache-push-service fails (WARN) when the unit is not active",
        r.get("status") == "FAIL" and r.get("severity") == "WARN" and UNIT in r.get("message", ""),
        f"{_row(rows, row)} {res.describe() if not rows else ''}",
    )
    stubs.systemctl("active")
    with cache_queue.PushQueue(box.queue_dir):
        pass
    _, rows = _doctor(box)
    smoke.check(
        "(k) cache-push-service passes with an active unit and an empty queue",
        rows.get(row, {}).get("status") == "PASS",
        _row(rows, row),
    )
    for minutes, sev, label in ((20, "INFO", "flags (FAIL/INFO) past 15"), (70, "WARN", "fails (FAIL/WARN) past 60")):
        with cache_queue.PushQueue(box.queue_dir) as q:
            q.remove(q.pending())
            q.enqueue(["e2e-old-1.0.tar.gz", "e2e-old-1.0.tar.gz.done"], now=time.time() - minutes * 60)
        _, rows = _doctor(box)
        r = rows.get(row, {})
        smoke.check(
            f"(k) cache-push-service {label} min ({minutes} min old entry)",
            r.get("status") == "FAIL" and r.get("severity") == sev and str(minutes) in r.get("message", ""),
            _row(rows, row),
        )
    stubs.systemctl("inactive")

    box.configure(**box.base())
    now = time.time()
    for i in range(12):
        p = box.dl / f"git2_e2e.example.com.repo{i}.git.tar.gz.tmp"
        p.write_bytes(b"partial")
        os.utime(p, (now - 7200, now - 7200))
    (box.dl / "git2_e2e.example.com.repo99.git.tar.gz.tmp").write_bytes(b"fresh")
    _, rows = _doctor(box)
    r = rows.get(row, {})
    smoke.check(
        "(k) cache-push-service is skipped with no cache_push_target", r.get("status") == "SKIP", _row(rows, row)
    )
    stale = rows.get("store-stale-tarballs", {})
    msg = stale.get("message", "")
    listed = re.findall(r"git2_e2e\.example\.com\.repo\d+\.git\.tar\.gz\.tmp", msg)
    smoke.check(
        "(k) store-stale-tarballs warns with count 12, lists exactly 10 names, omits the fresh file",
        stale.get("status") == "FAIL"
        and stale.get("severity") == "WARN"
        and msg.startswith("12 ")
        and len(listed) == 10
        and "repo99" not in msg,
        _row(rows, "store-stale-tarballs"),
    )
    box.configure(**box.base(cache_push_target=box.target))
    _, rows = _doctor(box)
    smoke.check(
        "(k) store-stale-tarballs is skipped when cache_push_target is set",
        rows.get("store-stale-tarballs", {}).get("status") == "SKIP",
        _row(rows, "store-stale-tarballs"),
    )

    shared = "sstate-mirror-shared-mount"
    box.configure(**box.base(sstate_mirrors="file://.* http://sstate.example.invalid/PATH"))
    _, rows = _doctor(box, nfs=True)
    r = rows.get(shared, {})
    smoke.check(
        "(k) sstate-mirror-shared-mount fails (BLOCK) for an http mirror over an NFS sstate directory",
        r.get("status") == "FAIL" and r.get("severity") == "BLOCK",
        _row(rows, shared),
    )
    box.configure(**box.base(sstate_mirrors="file://.* file:///srv/sstate-mirror/PATH"))
    _, rows = _doctor(box, nfs=True)
    smoke.check(
        "(k) sstate-mirror-shared-mount passes for a file:// mirror over the same directory",
        rows.get(shared, {}).get("status") == "PASS",
        _row(rows, shared),
    )


def scenario_l(smoke, root: Path, stubs: Stubs, hub: Hub) -> None:
    box = new_box(root, "l", stubs)
    hub.root = box.store
    six = {
        "dl_dir": str(box.dl),
        "sstate_dir": str(box.sstate),
        "sstate_mirrors": "file://.* file:///srv/sstate-mirror/PATH",
        "scheduler": "completion",
        "dl_mirror_url": hub.url,
        "cache_push_target": box.target,
    }
    box.configure(**six)
    res = bakar(box, "settings", "list", timeout=60)
    lines = set(res.out.splitlines())
    missing = [k for k, v in six.items() if f"build.{k} = {v!r}" not in lines]
    smoke.check(
        "(l) all six [build] string keys show in `bakar settings list` exactly as written",
        res.rc == 0 and not missing,
        f"missing={missing} {res.describe() if missing else ''}",
    )
    dry = bakar(box, "--hide-doctor-report", "build", "--dry-run", "kas.yml")
    env = _env_lines(dry)
    smoke.check(
        "(l) dry run resolves DL_DIR and SSTATE_DIR to dl_dir and sstate_dir (getvar stand-in)",
        dry.rc == 0 and env.get("DL_DIR") == six["dl_dir"] and env.get("SSTATE_DIR") == six["sstate_dir"],
        f"DL_DIR={env.get('DL_DIR')} SSTATE_DIR={env.get('SSTATE_DIR')} {dry.describe() if dry.rc else ''}",
    )
    box.configure(**{k: v for k, v in six.items() if k not in ("dl_mirror_url", "cache_push_target")})
    res2 = bakar(box, "settings", "list", timeout=60)
    lines2 = set(res2.out.splitlines())
    smoke.check(
        "(l) with dl_mirror_url and cache_push_target absent both show (unset) and no config error",
        res2.rc == 0
        and "build.dl_mirror_url = (unset)" in lines2
        and "build.cache_push_target = (unset)" in lines2
        and "Traceback" not in res2.text
        and "Error" not in res2.err,
        res2.describe() if res2.rc else "",
    )


_FETCH_TIMEOUT_RE = re.compile(r"^\s*(export\s+)?BB_FETCH_TIMEOUT(:[\w-]+)?\s*(\?\?=|\?=|:=|\+=|=\+|\.=|=\.|=)")


def _fetch_timeout_assignments(paths: list[Path]) -> list[str]:
    hits = []
    for path in paths:
        try:
            text = path.read_text(errors="replace")
        except OSError, UnicodeDecodeError:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if _FETCH_TIMEOUT_RE.match(line):
                hits.append(f"{path}:{n}")
    return hits


def scenario_m(smoke, root: Path) -> None:
    materialized = (
        _materialize(root, "generic", ["bakar-tuning-generic.yml", "bakar-tuning-hub-cache.yml"])
        + _materialize(root, "nxp", ["bakar-tuning-nxp.yml"])
        + _materialize(root, "ti", ["bakar-tuning-ti.yml"])
    )
    hits = _fetch_timeout_assignments(materialized)
    smoke.check(
        "(m) no materialized generic, NXP, TI or hub-cache overlay assigns BB_FETCH_TIMEOUT",
        len(materialized) == 4 and all(p.is_file() for p in materialized) and not hits,
        f"materialized={[str(p) for p in materialized]} hits={hits}",
    )
    shipped = [p for p in (REPO / "examples").rglob("*") if p.is_file()]
    shipped += [p for p in (SRC / "bakar" / "overlays").rglob("*") if p.is_file()]
    hits2 = _fetch_timeout_assignments(shipped)
    smoke.check(
        "(m) no shipped overlay or example file assigns BB_FETCH_TIMEOUT",
        bool(shipped) and not hits2,
        f"files scanned={len(shipped)} hits={hits2}",
    )


def main() -> int:
    smoke_lib = _load_smoke_lib()
    manual = [smoke_lib.ManualStep("human", text) for text in MANUAL_STEPS_TEXT]
    SCRATCH_PARENT.mkdir(parents=True, exist_ok=True)
    with (
        smoke_lib.SmokeRun(expected=EXPECTED_CHECKS, manual_steps=manual) as smoke,
        tempfile.TemporaryDirectory(prefix="smoke-hub-push-", dir=SCRATCH_PARENT) as tmp,
    ):
        root = Path(tmp)
        stubs = Stubs(root)
        # In-process drains resolve rsync through this process's PATH.
        os.environ["PATH"] = f"{stubs.dir}:{os.environ.get('PATH', '/usr/bin')}"
        hub = Hub()
        try:
            probe = new_box(root, "probe", stubs)
            origin = run_cmd([PY, "-c", "import bakar; print(bakar.__file__)"], probe.env, probe.ws, 60).out.strip()
            smoke.check(
                "bakar subprocesses import this worktree's src",
                bool(origin) and Path(origin).resolve().is_relative_to(SRC.resolve()),
                origin,
            )
            scenario_a(smoke, root, stubs, hub)
            scenario_b(smoke, root, stubs, hub)
            scenario_c(smoke, root, stubs, hub)
            scenario_d(smoke, root, stubs)
            scenario_e(smoke, root, stubs, hub)
            scenario_f(smoke, root, stubs)
            scenario_g(smoke, root, stubs)
            scenario_h(smoke, root, stubs)
            scenario_i(smoke, root, stubs, hub)
            scenario_j(smoke, root, stubs)
            scenario_k(smoke, root, stubs)
            scenario_l(smoke, root, stubs, hub)
            scenario_m(smoke, root)
        finally:
            hub.close()
        return smoke_lib.finish(smoke)


if __name__ == "__main__":
    sys.exit(main())
