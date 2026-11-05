#!/usr/bin/env python3
"""End-to-end smoke for the kas form of ``bakar sync`` and the meta-avocado ``bakar drift``.

Drives the installed ``bakar`` binary (the one on PATH, not this checkout's
source) against throwaway local git repositories under
``/var/tmp/claude-code/bakar/``. Nothing is fetched: every kas ``url`` is an
``example.invalid`` address and every pinned commit already exists in the clone.

Layout built under the scratch root:

- ``origins/<name>``: one upstream repository per pinned clone.
- ``meta-avocado``: a git checkout holding the kas files (the pin sync only
  accepts a kas file inside a meta-avocado checkout).
- ``ws-stale``: ``openembedded-core`` clean at its pin, ``meta-erlang`` clean
  on branch ``main`` one commit behind its pin, ``meta-missing`` never cloned.
- ``ws-dirty``: ``openembedded-core`` at its pin and ``meta-dirty`` off its pin
  with an uncommitted edit.
- ``ws-ahead``: ``meta-erlang`` one commit newer than its pin, ``meta-missing``
  never cloned (the drift scenario the archived usage notes describe).

Scenarios a-g follow the change's task 1.1. The remote-host steps (tasks 1.2
and 1.3) need pc3 and are printed as the manual remainder.

Usage:
    uv run python scripts/smoke_remote_dispatch_stale_workspace.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time

SMOKE_LIB_PATH = pathlib.Path("~/.claude/skills/ds-archive/scripts/smoke_lib.py").expanduser()
_spec = importlib.util.spec_from_file_location("smoke_lib", SMOKE_LIB_PATH)
if _spec is None or _spec.loader is None:
    raise SystemExit(f"cannot load {SMOKE_LIB_PATH}")
smoke_lib = importlib.util.module_from_spec(_spec)
sys.modules["smoke_lib"] = smoke_lib
_spec.loader.exec_module(smoke_lib)

SCRATCH_PARENT = pathlib.Path("/var/tmp/claude-code/bakar")
CMD_TIMEOUT_S = 180.0
LOCK_HOLD_S = 60
EXPECTED_CHECKS = 41

GIT_IDENT = ["-c", "user.email=smoke@example.invalid", "-c", "user.name=smoke"]
MISSING_PIN = "0123456789abcdef0123456789abcdef01234567"

MANUAL_STEPS = (
    smoke_lib.ManualStep(
        "human",
        "Task 1.2: on pc3, with the same bakar on both hosts and a meta-avocado worktree whose kas file pins "
        "meta-erlang older than pc3's clean clone, run `bakar sync $KAS --on pc3 --dry-run`, `bakar sync $KAS "
        "--on pc3`, then the printed `--revert <record> --on pc3`; after each step record `ssh pc3 git -C "
        "<workspace>/meta-erlang rev-parse HEAD` and `rev-parse --abbrev-ref HEAD`.",
    ),
    smoke_lib.ManualStep(
        "human",
        "Task 1.3: on pc3, `bakar build $KAS --on pc3 -t <recipe> --yes` with bitbake and openembedded-core "
        "pinned older must exit non-zero naming both clones and leave `for-each-ref refs/bakar/`, "
        "`.bakar/worktrees` and the workspace listing unchanged; `--dry-run` with only meta-erlang off pin "
        "must print a `workspace:` line and `dry run: nothing was pushed or created on pc3` and exit 0; a build "
        "holding bitbake.lock or repos-in-use.lock must make `bakar sync $KAS --on pc3` refuse naming it.",
    ),
)


def _run(argv: list[str], *, env: dict[str, str], cwd: pathlib.Path, out_dir: pathlib.Path) -> tuple[int, str]:
    """Run ``argv`` in its own session with output to a file; return (exit code, stdout+stderr)."""
    fd, _path = tempfile.mkstemp(prefix="cmd-", suffix=".log", dir=out_dir)
    with os.fdopen(fd, "w+") as out:
        child = subprocess.Popen(
            argv,
            stdout=out,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            cwd=cwd,
            env=env,
            start_new_session=True,
        )
        try:
            child.wait(timeout=CMD_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            _stop_group(child)
            out.seek(0)
            return 124, out.read() + f"\n[smoke] {argv[0]} timed out after {CMD_TIMEOUT_S}s"
        out.seek(0)
        return child.returncode, out.read()


def _stop_group(child: subprocess.Popen) -> None:
    """Terminate only ``child``'s own process group, and only when it leads one."""
    try:
        pgid = os.getpgid(child.pid)
    except ProcessLookupError:
        child.wait()
        return
    if pgid != child.pid:
        print(f"[smoke] refusing to signal pid {child.pid}: it does not lead its own process group")
        child.wait()
        return
    os.killpg(pgid, signal.SIGTERM)
    try:
        child.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(pgid, signal.SIGKILL)
        child.wait()


class Fixture:
    def __init__(self, root: pathlib.Path, env: dict[str, str]) -> None:
        self.root = root
        self.env = {**env, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
        self.logs = root / "logs"
        self.logs.mkdir()

    def git(self, *args: str) -> str:
        rc, out = _run(["git", *GIT_IDENT, *args], env=self.env, cwd=self.root, out_dir=self.logs)
        if rc != 0:
            raise RuntimeError(f"git {' '.join(args)} failed ({rc}): {out.strip()}")
        return out.strip()

    def git_rc(self, *args: str) -> tuple[int, str]:
        rc, out = _run(["git", *GIT_IDENT, *args], env=self.env, cwd=self.root, out_dir=self.logs)
        return rc, out.strip()

    def bakar(self, *args: str) -> tuple[int, str]:
        rc, out = _run(["bakar", *args], env=self.env, cwd=self.root, out_dir=self.logs)
        print(f"$ bakar {' '.join(args)}  -> exit {rc}")
        for line in out.rstrip().splitlines():
            print(f"    {line}")
        return rc, out

    def head(self, repo: pathlib.Path) -> str | None:
        if not repo.is_dir():
            return None
        return self.git("-C", str(repo), "rev-parse", "HEAD")

    def branch(self, repo: pathlib.Path) -> str | None:
        rc, out = self.git_rc("-C", str(repo), "symbolic-ref", "-q", "HEAD")
        return out if rc == 0 else None

    def origin(self, name: str, *contents: str) -> list[str]:
        """Create ``origins/<name>`` with one commit per ``contents`` on ``main``; return the shas."""
        repo = self.root / "origins" / name
        self.git("init", "-q", "-b", "main", str(repo))
        shas = []
        for index, text in enumerate(contents):
            (repo / "f").write_text(text)
            self.git("-C", str(repo), "add", "-A")
            self.git("-C", str(repo), "commit", "-q", "-m", f"c{index}")
            shas.append(self.git("-C", str(repo), "rev-parse", "HEAD"))
        return shas

    def clone(self, name: str, ws: pathlib.Path, at: str) -> pathlib.Path:
        """Clone ``origins/<name>`` into ``ws`` with branch ``main`` reset to ``at``."""
        dest = ws / name
        self.git("clone", "-q", str(self.root / "origins" / name), str(dest))
        self.git("-C", str(dest), "reset", "-q", "--hard", at)
        return dest


def _kas_text(pins: dict[str, str]) -> str:
    lines = ["header:", "  version: 16", "machine: qemux86-64", "repos:", "  meta-avocado:", "    path: meta-avocado"]
    for name, pin in pins.items():
        branch = "wrynose-avocado" if name == "openembedded-core" else "main"
        lines += [
            f"  {name}:",
            f"    url: https://example.invalid/{name}.git",
            f"    branch: {branch}",
            f"    commit: {pin}",
        ]
    return "\n".join(lines) + "\n"


def _ls_a(path: pathlib.Path) -> list[str] | None:
    return sorted(os.listdir(path)) if path.is_dir() else None


def _wait_for(path: pathlib.Path, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.1)
    return False


def main() -> None:
    SCRATCH_PARENT.mkdir(parents=True, exist_ok=True)
    tempfile.tempdir = str(SCRATCH_PARENT)
    with smoke_lib.SmokeRun(EXPECTED_CHECKS, manual_steps=MANUAL_STEPS) as run:
        run.check(
            "kas is on PATH (bakar reads the pins with kas dump)",
            shutil.which("kas") is not None,
            "kas not found; the pin sync cannot read a kas file without it",
        )
        run.check("bakar is on PATH", shutil.which("bakar") is not None, "bakar not found")
        with smoke_lib.isolated_env("stale-ws-smoke-") as (root, env):
            fx = Fixture(root, env)
            _scenarios(run, fx)
    sys.exit(smoke_lib.finish(run))


def _scenarios(run, fx: Fixture) -> None:
    root = fx.root
    (oe_pin,) = fx.origin("openembedded-core", "oe")
    erl_old, erl_pin = fx.origin("meta-erlang", "old", "pin")
    dirty_old, dirty_pin = fx.origin("meta-dirty", "old", "pin")

    checkout = root / "meta-avocado"
    (checkout / "kas" / "machine").mkdir(parents=True)
    fx.git("init", "-q", "-b", "main", str(checkout))
    stale_kas = checkout / "kas" / "machine" / "stale.yml"
    dirty_kas = checkout / "kas" / "machine" / "dirty.yml"
    stale_kas.write_text(_kas_text({"openembedded-core": oe_pin, "meta-erlang": erl_pin, "meta-missing": MISSING_PIN}))
    dirty_kas.write_text(_kas_text({"openembedded-core": oe_pin, "meta-dirty": dirty_pin}))
    fx.git("-C", str(checkout), "add", "-A")
    fx.git("-C", str(checkout), "commit", "-q", "-m", "kas files")

    ws = root / "ws-stale"
    ws.mkdir()
    oe = fx.clone("openembedded-core", ws, oe_pin)
    erl = fx.clone("meta-erlang", ws, erl_old)
    missing = ws / "meta-missing"
    clones = {"openembedded-core": oe, "meta-erlang": erl, "meta-missing": missing}
    kas = str(stale_kas)
    record_dir = ws / ".bakar" / "sync-revert"

    # (a) the dry run predicts and changes nothing.
    before_heads = {name: fx.head(path) for name, path in clones.items()}
    before_ls = _ls_a(ws / ".bakar")
    rc, out = fx.bakar("sync", kas, "--dry-run", "-w", str(ws))
    run.check("(a) dry run exits 0", rc == 0, f"exit {rc}")
    run.check(
        "(a) dry run: meta-erlang would move",
        f"meta-erlang: would move to {erl_pin[:12]}" in out,
        "no 'would move' line for meta-erlang",
    )
    run.check(
        "(a) dry run: openembedded-core already at its pin",
        "openembedded-core: already at its pin" in out,
        "no 'already at its pin' line",
    )
    run.check("(a) dry run: meta-missing reported missing", "meta-missing: missing" in out, "no 'missing' line")
    run.check(
        "(a) dry run prints no batch complete or revert record line",
        "batch complete" not in out and "revert record" not in out,
        "found a sync-only line",
    )
    after_heads = {name: fx.head(path) for name, path in clones.items()}
    run.check(
        "(a) every clone HEAD identical after the dry run",
        before_heads == after_heads,
        f"{before_heads} != {after_heads}",
    )
    run.check(
        "(a) ls -a <ws>/.bakar identical after the dry run",
        before_ls == _ls_a(ws / ".bakar"),
        f"{before_ls} != {_ls_a(ws / '.bakar')}",
    )

    # (b) the real sync moves only the clean off-pin clone.
    rc, out = fx.bakar("sync", kas, "-w", str(ws))
    run.check("(b) sync exits 0", rc == 0, f"exit {rc}")
    moved = [line for line in out.splitlines() if line.startswith("MOVED ")]
    run.check(
        "(b) exactly one MOVED line, for meta-erlang",
        len(moved) == 1 and moved[0].startswith("MOVED meta-erlang "),
        f"MOVED lines: {moved}",
    )
    run.check("(b) meta-erlang HEAD is now its pin", fx.head(erl) == erl_pin, f"HEAD {fx.head(erl)}")
    run.check("(b) meta-erlang HEAD is detached", fx.branch(erl) is None, f"still on {fx.branch(erl)}")
    run.check(
        "(b) branch main keeps the old commit",
        fx.git("-C", str(erl), "rev-parse", "refs/heads/main") == erl_old,
        "refs/heads/main moved",
    )
    run.check(
        "(b) openembedded-core untouched and meta-missing still absent",
        fx.head(oe) == oe_pin and not missing.exists(),
        f"oe {fx.head(oe)}, missing exists={missing.exists()}",
    )
    batch = re.search(r"batch complete: (\d+)/(\d+) repos, moved=1 failed=(\d+)", out)
    run.check(
        "(b) prints batch complete: N/N repos, moved=1 failed=<n>",
        batch is not None and batch.group(1) == batch.group(2),
        f"batch line: {batch and batch.group(0)}",
    )
    rec = re.search(r"revert record: (\S+)", out)
    record = pathlib.Path(rec.group(1)) if rec else None
    run.check(
        "(b) prints a revert record path under <ws>/.bakar/sync-revert/ that exists",
        record is not None and record.parent == record_dir and record.is_file(),
        f"record {record}",
    )
    rc2, out2 = fx.bakar("sync", kas, "-w", str(ws))
    run.check(
        "(b) an immediate second sync exits 0 with no MOVED line",
        rc2 == 0 and not any(line.startswith("MOVED") for line in out2.splitlines()),
        f"exit {rc2}",
    )

    # (c) the revert puts the branch and HEAD back.
    rc, out = fx.bakar("sync", "--revert", str(record), "-w", str(ws)) if record else (-1, "")
    run.check("(c) revert with -w exits 2 (--revert refuses -w)", rc == 2 and "-w" in out, f"exit {rc}")
    rc, out = fx.bakar("sync", "--revert", str(record)) if record else (-1, "")
    run.check("(c) revert exits 0", rc == 0, f"exit {rc}")
    run.check(
        "(c) meta-erlang is back on refs/heads/main", fx.branch(erl) == "refs/heads/main", f"branch {fx.branch(erl)}"
    )
    run.check("(c) meta-erlang HEAD is the recorded old commit", fx.head(erl) == erl_old, f"HEAD {fx.head(erl)}")

    # (f) drift reports the behind-pin clone and the missing clone.
    rc, out = fx.bakar("drift", kas, "--json", "-w", str(ws))
    rows = _json_rows(out)
    by_name = {row.get("source"): row for row in rows}
    run.check("(f) drift exits 1 (rows drifted)", rc == 1, f"exit {rc}")
    erl_row = by_name.get("meta-erlang", {})
    run.check(
        "(f) behind-pin meta-erlang: drifted, distance -1 (behind counts negative)",
        erl_row.get("drifted") is True and erl_row.get("distance") == -1,
        f"row {erl_row}",
    )
    miss_row = by_name.get("meta-missing", {})
    run.check(
        "(f) meta-missing: missing true, drifted true",
        miss_row.get("missing") is True and miss_row.get("drifted") is True,
        f"row {miss_row}",
    )

    ws_ahead = root / "ws-ahead"
    ws_ahead.mkdir()
    ahead = fx.clone("meta-erlang", ws_ahead, erl_pin)
    (ahead / "f").write_text("newer than the pin")
    fx.git("-C", str(ahead), "commit", "-q", "-am", "ahead")
    rc, out = fx.bakar("drift", kas, "--json", "-w", str(ws_ahead))
    by_name = {row.get("source"): row for row in _json_rows(out)}
    ahead_row = by_name.get("meta-erlang", {})
    run.check(
        "(f) newer-than-pin meta-erlang: drifted, missing false, positive distance",
        ahead_row.get("drifted") is True
        and ahead_row.get("missing") is False
        and isinstance(ahead_row.get("distance"), int)
        and ahead_row["distance"] > 0,
        f"row {ahead_row}",
    )
    run.check(
        "(f) ahead workspace: meta-missing reported missing",
        by_name.get("meta-missing", {}).get("missing") is True,
        f"rows {list(by_name)}",
    )

    # (d) a kas file with a manifest flag is a usage error and writes nothing.
    records_before = _ls_a(record_dir)
    rc, out = fx.bakar("sync", kas, "-f", "imx-6.6.52-2.2.0.xml")
    run.check("(d) kas file with -f exits 2", rc == 2, f"exit {rc}")
    run.check("(d) refusal names --manifest", "--manifest" in out, "no --manifest in the output")
    run.check(
        "(d) no revert record printed or written",
        "revert record" not in out and _ls_a(record_dir) == records_before,
        "a revert record appeared",
    )
    run.check("(d) meta-erlang not moved by the refused sync", fx.head(erl) == erl_old, f"HEAD {fx.head(erl)}")

    # (e) a holder of the in-use lock refuses the sync.
    lock = ws / ".bakar" / "repos-in-use.lock"
    ready = fx.root / "lock-held"
    holder = subprocess.Popen(
        ["flock", "--shared", str(lock), "sh", "-c", f'touch "$1"; exec sleep {LOCK_HOLD_S}', "sh", str(ready)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        held = _wait_for(ready, 10)
        run.check("(e) a process holds flock --shared on repos-in-use.lock", held, "holder never signalled")
        rc, out = fx.bakar("sync", kas, "-w", str(ws))
        run.check("(e) sync exits 1 while the lock is held", rc == 1, f"exit {rc}")
        run.check("(e) refusal names repos-in-use.lock", "repos-in-use.lock" in out, "lock not named")
        run.check(
            "(e) nothing moved while the lock is held",
            fx.head(erl) == erl_old and not any(line.startswith("MOVED") for line in out.splitlines()),
            f"HEAD {fx.head(erl)}",
        )
    finally:
        _stop_group(holder)
    rc, out = fx.bakar("sync", kas, "-w", str(ws))
    run.check(
        "(e) sync succeeds once the holder exits",
        rc == 0 and fx.head(erl) == erl_pin,
        f"exit {rc}, HEAD {fx.head(erl)}",
    )

    # (g) a dirty off-pin clone is left alone.
    ws_dirty = root / "ws-dirty"
    ws_dirty.mkdir()
    fx.clone("openembedded-core", ws_dirty, oe_pin)
    dirty = fx.clone("meta-dirty", ws_dirty, dirty_old)
    (dirty / "f").write_text("local edit")
    status_before = fx.git("-C", str(dirty), "status", "--porcelain")
    rc, out = fx.bakar("sync", str(dirty_kas), "--dry-run", "-w", str(ws_dirty))
    run.check(
        "(g) dry run reports meta-dirty left alone and exits 1",
        rc == 1 and "meta-dirty: left alone because of local changes" in out,
        f"exit {rc}",
    )
    rc, out = fx.bakar("sync", str(dirty_kas), "-w", str(ws_dirty))
    run.check("(g) sync exits 1 with the dirty clone", rc == 1, f"exit {rc}")
    run.check("(g) sync reports LEFT meta-dirty", "LEFT meta-dirty has local changes, not moved" in out, "no LEFT line")
    run.check(
        "(g) dirty clone HEAD, branch and edit untouched",
        fx.head(dirty) == dirty_old
        and fx.branch(dirty) == "refs/heads/main"
        and (dirty / "f").read_text() == "local edit"
        and fx.git("-C", str(dirty), "status", "--porcelain") == status_before,
        f"HEAD {fx.head(dirty)}, branch {fx.branch(dirty)}",
    )
    batch = re.search(r"batch complete: (\d+)/(\d+) repos, moved=0 failed=(\d+)", out)
    run.check(
        "(g) batch complete line shows moved=0",
        batch is not None and batch.group(1) == batch.group(2),
        f"batch line: {batch and batch.group(0)}",
    )


def _json_rows(out: str) -> list[dict]:
    # stderr is merged into out, and a config warning such as "unknown key ... in [build]"
    # holds a "[" too, so the JSON array is the first line that is only an opening bracket.
    match = re.search(r"^\[\s*$", out, re.MULTILINE)
    if match is None:
        return []
    try:
        rows = json.loads(out[match.start() :])
    except json.JSONDecodeError:
        return []
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


if __name__ == "__main__":
    main()
