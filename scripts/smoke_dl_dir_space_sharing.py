#!/usr/bin/env python3
"""Smoke for the CLI-drivable scenarios of dl-dir-space-sharing.

Drives this checkout's ``bakar`` (``python -m bakar`` with ``src`` on
``PYTHONPATH``) as a subprocess against scratch directories and a stub hub that
binds 127.0.0.1 only. Every scenario runs with ``HOME`` and the XDG variables
pointed into the scratch tree, so the real ``~/.config/bakar/config.toml`` is
never read, and ``DL_DIR`` / ``BAKAR_DL_MIRROR_URL`` are set or removed
explicitly per scenario.

Scenarios:

- (A) an invalid ``build.dl_mirror_paths`` entry makes bakar exit non-zero naming
  the key and the entry. The checkout has no ``bakar config show``; every
  command loads the user config first, so ``bakar settings list`` stands in.
- (B) ``prune-downloads`` with no ``DL_DIR`` and no ``build.dl_dir`` exits
  non-zero naming both and prints no class block.
- (C) a dry run exits 0, prints one block per class and leaves every path alone.
- (D) ``--apply`` against the stub hub removes only the verified payload and its
  stamp; a symbolic link and its target survive.
- (E) a ``git2_<n>.tar.gz`` beside a complete clone goes, the clone stays, and a
  tarball with no clone stays.
- (F) ``--clone-older-than 30`` removes only the 40-day-old clone.
- (G) a lock held by another process keeps its item ``in use`` while an idle
  item and its lock file are removed.

Usage:
    uv run python scripts/smoke_dl_dir_space_sharing.py
"""

from __future__ import annotations

import http.server
import importlib.util
import os
import pathlib
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time

# A copy of smoke_lib.py beside this script wins, so the live-gate run on a host
# without the Claude skills tree (the builder) can ship the two files together.
_LIB_CANDIDATES = (
    pathlib.Path(__file__).resolve().parent / "smoke_lib.py",
    pathlib.Path("~/.claude/skills/ds-archive/scripts/smoke_lib.py").expanduser(),
)
SMOKE_LIB_PATH = next((p for p in _LIB_CANDIDATES if p.is_file()), _LIB_CANDIDATES[-1])
_spec = importlib.util.spec_from_file_location("smoke_lib", SMOKE_LIB_PATH)
if _spec is None or _spec.loader is None:
    raise SystemExit(f"cannot load {SMOKE_LIB_PATH}")
smoke_lib = importlib.util.module_from_spec(_spec)
sys.modules["smoke_lib"] = smoke_lib
_spec.loader.exec_module(smoke_lib)

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRATCH_PARENT = pathlib.Path("/var/tmp/claude-code/bakar")
EXPECTED_CHECKS = 34
BAKAR_TIMEOUT_S = 120
DAY_S = 86400

MANUAL_STEPS = (
    smoke_lib.ManualStep(
        "human",
        "On a real builder, run `bakar doctor`: the `dl-mirror-paths-ready` row is PASS with the mount up, and the "
        "`dl-dir-size` row is WARN once DL_DIR is above 60 GB.",
    ),
    smoke_lib.ManualStep(
        "human",
        "On a real builder with the hub store mounted, run a host-mode fetch and confirm it leaves a symbolic link "
        "into the mounted store in DL_DIR, not a copy.",
    ),
    smoke_lib.ManualStep(
        "human",
        "Block the hub (mount down or unreachable) and confirm the stall documented in docs/features/dl-mirror.md "
        "appears as described.",
    ),
)

_CLASS_BLOCK = re.compile(r"^(hub|tarball|clone): ", re.MULTILINE)


class Hub:
    """A HEAD-only stub hub: 2xx with the table's length, 404 for names not in it."""

    def __init__(self, table: dict[str, int]) -> None:
        self.table = table
        table_ref = table

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_HEAD(self) -> None:
                name = self.path.lstrip("/")
                if name.endswith(".done"):
                    length = 0 if name[: -len(".done")] in table_ref else None
                else:
                    length = table_ref.get(name)
                if length is None:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                else:
                    self.send_response(200)
                    self.send_header("Content-Length", str(length))
                self.end_headers()

            def log_message(self, format: str, *args: object) -> None:
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self) -> Hub:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def scenario_env(base: dict[str, str], root: pathlib.Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    """A bakar environment confined to ``root``: no inherited bakar, proxy or download settings."""
    env = {
        k: v
        for k, v in base.items()
        if not k.startswith(("BAKAR_", "DL_", "SSTATE_"))
        and k.lower() not in ("http_proxy", "https_proxy", "all_proxy")
    }
    home = root / "home"
    home.mkdir(exist_ok=True)
    env["HOME"] = str(home)
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    env["COLUMNS"] = "1000"
    env["NO_COLOR"] = "1"
    env["NO_PROXY"] = "127.0.0.1"
    env.update(extra or {})
    return env


def bakar(root: pathlib.Path, env: dict[str, str], *args: str) -> tuple[int, str]:
    work = root / "work"
    work.mkdir(exist_ok=True)
    proc = subprocess.run(
        [sys.executable, "-m", "bakar", *args],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        timeout=BAKAR_TIMEOUT_S,
        check=False,
    )
    return proc.returncode, proc.stdout + proc.stderr


def write(path: pathlib.Path, size: int = 0, mtime: float | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def snapshot(tree: pathlib.Path) -> dict[str, tuple[int, int, int]]:
    out: dict[str, tuple[int, int, int]] = {}
    for dirpath, dirs, files in os.walk(tree, followlinks=False):
        for name in dirs + files:
            p = os.path.join(dirpath, name)
            st = os.lstat(p)
            out[p] = (st.st_mode, st.st_size, st.st_mtime_ns)
    return out


def present(*paths: pathlib.Path) -> bool:
    return all(os.path.lexists(p) for p in paths)


def absent(*paths: pathlib.Path) -> bool:
    return not any(os.path.lexists(p) for p in paths)


def block(out: str, kind: str) -> str:
    """The report lines of one class block (its header plus indented lines)."""
    lines = out.splitlines()
    for i, line in enumerate(lines):
        if line.startswith(f"{kind}: "):
            end = i + 1
            while end < len(lines) and lines[end].startswith("  "):
                end += 1
            return "\n".join(lines[i:end])
    return ""


def hub_fixture(root: pathlib.Path, name: str) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path, dict[str, int]]:
    """A downloads dir with ok/gone/big payloads and a symlinked payload; returns (dl, target, target_stamp, table)."""
    dl = root / name
    for payload in ("ok-1.0.tar.gz", "gone-1.0.tar.gz", "big-1.0.tar.gz"):
        write(dl / payload, 1000)
        write(dl / (payload + ".done"))
    outside = root / (name + "-outside")
    target = outside / "target.bin"
    target_stamp = outside / "target.bin.done"
    write(target, 1000)
    write(target_stamp)
    (dl / "link-1.0.tar.gz").symlink_to(target)
    (dl / "link-1.0.tar.gz.done").symlink_to(target_stamp)
    # The hub would verify the link's name too: it must still be left alone.
    table = {"ok-1.0.tar.gz": 1000, "big-1.0.tar.gz": 999999, "link-1.0.tar.gz": 1000}
    return dl, target, target_stamp, table


def scenario_a(run, root: pathlib.Path, base_env: dict[str, str]) -> None:
    config = root / "home" / ".config" / "bakar"
    config.mkdir(parents=True)
    entry = "/srv/dl/../etc"
    (config / "config.toml").write_text(f'config_version = 1\n[build]\ndl_mirror_paths = "{entry}"\n')
    code, out = bakar(root, scenario_env(base_env, root), "settings", "list")
    run.check("(A) invalid dl_mirror_paths exits non-zero", code != 0, f"exit {code}: {out[-300:]}")
    run.check("(A) output names the key", "dl_mirror_paths" in out, out[-300:])
    run.check("(A) output names the entry", entry in out, out[-300:])


def scenario_b(run, root: pathlib.Path, base_env: dict[str, str]) -> None:
    code, out = bakar(root, scenario_env(base_env, root), "cache", "prune-downloads")
    run.check("(B) no DL_DIR and no dl_dir exits non-zero", code != 0, f"exit {code}: {out[-300:]}")
    run.check("(B) output names DL_DIR", "DL_DIR" in out, out[-300:])
    run.check("(B) output names build.dl_dir", "build.dl_dir" in out, out[-300:])
    run.check("(B) no class block is printed", _CLASS_BLOCK.search(out) is None, out[-300:])


def scenario_cd(run, root: pathlib.Path, base_env: dict[str, str]) -> None:
    dl, target, target_stamp, table = hub_fixture(root, "dl-hub")
    with Hub(table) as hub:
        env = scenario_env(base_env, root, {"DL_DIR": str(dl), "BAKAR_DL_MIRROR_URL": hub.url})
        before = snapshot(root)
        code, out = bakar(root, env, "cache", "prune-downloads")
        after = snapshot(root)
        run.check("(C) dry run exits 0", code == 0, f"exit {code}: {out[-300:]}")
        run.check(
            "(C) dry run prints one block per class",
            all(len(_CLASS_BLOCK.findall(out)) == 3 and f"{k}: " in out for k in ("hub", "tarball", "clone")),
            out[-300:],
        )
        run.check("(C) dry run would remove 1 hub item", "would remove 1 item(s)" in block(out, "hub"), out[-300:])
        run.check("(C) dry run says nothing was removed", "nothing was removed" in out, out[-300:])
        run.check(
            "(C) dry run leaves every path in place",
            before == after,
            f"changed: {sorted(set(before) ^ set(after))[:5]}",
        )

        code, out = bakar(root, env, "cache", "prune-downloads", "--apply")
        run.check("(D) --apply exits 0", code == 0, f"exit {code}: {out[-300:]}")
        run.check(
            "(D) ok payload and its stamp are removed", absent(dl / "ok-1.0.tar.gz", dl / "ok-1.0.tar.gz.done"), ""
        )
        run.check(
            "(D) gone and big (404, size mismatch) are kept with their stamps",
            present(
                dl / "gone-1.0.tar.gz",
                dl / "gone-1.0.tar.gz.done",
                dl / "big-1.0.tar.gz",
                dl / "big-1.0.tar.gz.done",
            ),
            "a kept item is missing",
        )
        run.check(
            "(D) the symbolic links and their targets survive",
            (dl / "link-1.0.tar.gz").is_symlink()
            and (dl / "link-1.0.tar.gz.done").is_symlink()
            and present(target, target_stamp),
            "a link or its target is gone",
        )
        run.check("(D) report says removed 1 hub item", "removed 1 item(s)" in block(out, "hub"), out[-300:])


def scenario_e(run, root: pathlib.Path, base_env: dict[str, str]) -> None:
    dl = root / "dl-git"
    for tar in ("git2_a.tar.gz", "git2_b.tar.gz"):
        write(dl / tar, 500)
        write(dl / (tar + ".done"))
    write(dl / "git2" / "a" / "HEAD", 10)
    write(dl / "git2" / "a" / "objects" / "pack", 100)
    write(dl / "git2" / "a.done")
    env = scenario_env(base_env, root, {"DL_DIR": str(dl)})
    code, out = bakar(root, env, "cache", "prune-downloads", "--apply")
    run.check("(E) --apply exits 0", code == 0, f"exit {code}: {out[-300:]}")
    run.check("(E) git2_a tarball and stamp are removed", absent(dl / "git2_a.tar.gz", dl / "git2_a.tar.gz.done"), "")
    run.check(
        "(E) the clone beside it stays complete",
        present(
            dl / "git2" / "a",
            dl / "git2" / "a" / "HEAD",
            dl / "git2" / "a" / "objects" / "pack",
            dl / "git2" / "a.done",
        ),
        "clone or its stamp is gone",
    )
    run.check(
        "(E) git2_b with no clone stays",
        present(dl / "git2_b.tar.gz", dl / "git2_b.tar.gz.done"),
        "git2_b was removed",
    )
    run.check("(E) clone class is not requested without the flag", "clone: not requested" in out, out[-300:])


def scenario_f(run, root: pathlib.Path, base_env: dict[str, str]) -> None:
    dl = root / "dl-clone"
    now = time.time()
    for name, age_days in (("old", 40), ("new", 1)):
        write(dl / "git2" / name / "HEAD", 10)
        write(dl / "git2" / (name + ".done"), mtime=now - age_days * DAY_S)
    env = scenario_env(base_env, root, {"DL_DIR": str(dl)})
    code, out = bakar(root, env, "cache", "prune-downloads", "--apply", "--clone-older-than", "30")
    run.check("(F) --apply --clone-older-than 30 exits 0", code == 0, f"exit {code}: {out[-300:]}")
    run.check(
        "(F) the 40-day-old clone and its stamp are removed", absent(dl / "git2" / "old", dl / "git2" / "old.done"), ""
    )
    run.check(
        "(F) no half-removed clone is left behind",
        not [p for p in (dl / "git2").iterdir() if p.name.endswith(".pruned.tmp")],
        "a .pruned.tmp is left",
    )
    run.check("(F) the 1-day-old clone stays", present(dl / "git2" / "new", dl / "git2" / "new.done"), "new clone gone")


def scenario_g(run, root: pathlib.Path, base_env: dict[str, str]) -> None:
    dl = root / "dl-lock"
    for payload in ("busy-1.0.tar.gz", "idle-1.0.tar.gz"):
        write(dl / payload, 1000)
        write(dl / (payload + ".done"))
    busy_lock = dl / "busy-1.0.tar.gz.lock"
    idle_lock = dl / "idle-1.0.tar.gz.lock"
    holder_code = (
        "import fcntl, os, sys, time\n"
        "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o644)\n"
        "fcntl.flock(fd, fcntl.LOCK_EX)\n"
        "print('locked', flush=True)\n"
        "time.sleep(600)\n"
    )
    log_path = root / "holder.log"
    holder = None
    try:
        with log_path.open("wb") as log:
            holder = subprocess.Popen(
                [sys.executable, "-c", holder_code, str(busy_lock)],
                stdout=log,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and "locked" not in log_path.read_text():
            time.sleep(0.05)
        run.check("(G) a child process holds the busy lock", "locked" in log_path.read_text(), log_path.read_text())
        table = {"busy-1.0.tar.gz": 1000, "idle-1.0.tar.gz": 1000}
        with Hub(table) as hub:
            env = scenario_env(base_env, root, {"DL_DIR": str(dl), "BAKAR_DL_MIRROR_URL": hub.url})
            code, out = bakar(root, env, "cache", "prune-downloads", "--apply")
        run.check("(G) --apply exits 0", code == 0, f"exit {code}: {out[-300:]}")
        run.check(
            "(G) the locked item and its stamp stay",
            present(dl / "busy-1.0.tar.gz", dl / "busy-1.0.tar.gz.done"),
            "busy item removed while locked",
        )
        run.check("(G) the locked item is reported in use", "kept 1: in use" in block(out, "hub"), out[-300:])
        run.check(
            "(G) the idle item and its stamp are removed",
            absent(dl / "idle-1.0.tar.gz", dl / "idle-1.0.tar.gz.done"),
            "",
        )
        run.check("(G) the idle item's lock file is removed", absent(idle_lock), "idle lock file left")
        run.check("(G) the held lock file stays", present(busy_lock), "held lock file removed")
    finally:
        stopped = holder is None
        if holder is not None:
            pgid = holder.pid
            # Only the child's own group: it was started with start_new_session=True.
            if os.getpgid(pgid) == pgid and pgid != os.getpgrp():
                os.killpg(pgid, signal.SIGTERM)
            try:
                holder.wait(timeout=10)
                stopped = True
            except subprocess.TimeoutExpired:
                os.killpg(pgid, signal.SIGKILL)
                holder.wait(timeout=10)
                stopped = True
        run.check("(G) the lock holder was stopped", stopped, "holder still running")


def main() -> int:
    SCRATCH_PARENT.mkdir(parents=True, exist_ok=True)
    tempfile.tempdir = str(SCRATCH_PARENT)
    with (
        smoke_lib.SmokeRun(EXPECTED_CHECKS, manual_steps=MANUAL_STEPS) as run,
        smoke_lib.isolated_env("dl-dir-space-sharing-smoke-") as (root, base_env),
    ):
        # Scenario A writes an invalid config, so it gets its own HOME.
        invalid_root = root / "invalid-config"
        invalid_root.mkdir()
        scenario_a(run, invalid_root, base_env)
        scenario_b(run, root, base_env)
        scenario_cd(run, root, base_env)
        scenario_e(run, root, base_env)
        scenario_f(run, root, base_env)
        scenario_g(run, root, base_env)
    return smoke_lib.finish(run)


if __name__ == "__main__":
    sys.exit(main())
