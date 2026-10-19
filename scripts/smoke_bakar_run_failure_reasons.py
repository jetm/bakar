#!/usr/bin/env python3
"""End-to-end smoke for the bakar-run-failure-reasons change.

Seeds run directories under an isolated HOME, XDG and workspace tree, then runs
``bakar status``, ``bakar wait``, ``bakar triage`` and ``bakar stop`` as
subprocesses of this worktree's code and checks what each prints and how each
exits. The steps that need a real kas build, a live systemd unit or a remote
build host are printed as the manual remainder.

Usage:
    uv run --frozen python scripts/smoke_bakar_run_failure_reasons.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"
SMOKE_LIB = Path.home() / ".claude" / "skills" / "ds-archive" / "scripts" / "smoke_lib.py"

ERROR_TEXT = "ERROR: Nothing PROVIDES 'does-not-exist'"
STOPPED_STEP = "stopped"
STOPPED_ERROR = "stopped by bakar stop"
KAS_YAML = "header:\n  version: 14\nmachine: qemux86-64\ndistro: poky\ntarget: core-image-minimal\n"
# Run ids end in a non-numeric tail so status never mistakes it for a front-end pid.
RUN_IDS = {k: f"20261019-1200{i:02d}-smoke{k}" for i, k in enumerate("abcdefh")}
CMD_TIMEOUT = 60

MANUAL_STEPS_TEXT = (
    "Real stop against a detached build: `bakar build kas.yml --target core-image-minimal --detach`, then "
    "`bakar stop kas.yml --run <run_id>`, then `bakar wait <handle> --timeout 60` and `bakar status <handle> "
    "--json`. The run directory must hold stop-request.json before the build ends, wait must exit 1, and failure "
    'must read step "stopped" and error "stopped by bakar stop". A person runs this because it starts a real kas '
    "build.",
    "Unwritable directory and external stop: `chmod a-w <run_dir>; bakar stop kas.yml --run <run_id>` must still "
    "stop the build and create no stop-request.json, and `systemctl --user stop <unit>` followed by `bakar status "
    '<handle> --json` must show no stop-request.json and a failure step other than "stopped". A person runs this '
    "because both need a live build and a real unit.",
    "Remote cases: `bakar stop --on HOST` must leave <run_dir>/stop-request.json in each registered run directory "
    "before the unit is signalled, none for a unit with no registered run, and none for a registry entry naming a "
    "non-directory; then `bakar status <handle> --on HOST --json` must show the same failure object plus "
    '"host": "HOST". A person runs this because it writes to the build host.',
    "Real error capture: `bakar build kas.yml --target does-not-exist --detach`, a kas file with a missing include, "
    "and a target whose ERROR line exceeds 200 characters with colour codes. `bakar status <handle> --json` must show "
    'failure.error as one "ERROR: " line of at most 200 characters with no escape characters, and wait must exit 1. '
    "A person runs this because each needs a real kas and bitbake run.",
)


def _load_smoke_lib():
    spec = importlib.util.spec_from_file_location("smoke_lib", SMOKE_LIB)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {SMOKE_LIB}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["smoke_lib"] = module
    spec.loader.exec_module(module)
    return module


def run(
    args: list[str], env: dict[str, str], cwd: Path, timeout: int = CMD_TIMEOUT
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args, env=env, cwd=cwd, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=timeout, check=False
    )


def bakar(env: dict[str, str], cwd: Path, *args: str, timeout: int = CMD_TIMEOUT) -> subprocess.CompletedProcess[str]:
    return run([sys.executable, "-m", "bakar", *args], env, cwd, timeout)


def py(env: dict[str, str], code: str, *args: str) -> str:
    """Run ``code`` against this worktree's bakar package; return stdout or raise with stderr."""
    proc = run([sys.executable, "-c", code, *args], env, REPO)
    if proc.returncode != 0:
        raise RuntimeError(f"helper failed: {proc.stderr.strip()}")
    return proc.stdout


def write_events(run_dir: Path, *recs: dict[str, object]) -> None:
    with (run_dir / "events.jsonl").open("a") as fh:
        for rec in recs:
            fh.write(json.dumps(rec) + "\n")


def seed_run(ws: Path, run_id: str, *recs: dict[str, object]) -> Path:
    run_dir = ws / "build" / "runs" / run_id
    run_dir.mkdir(parents=True)
    write_events(run_dir, {"ts": "2026-10-19T12:00:00Z", "event": "run_start"}, *recs)
    (run_dir / "kas.log").write_text("NOTE: seeded\n")
    return run_dir


def step_fail(**extra: object) -> dict[str, object]:
    return {"ts": "2026-10-19T12:00:05Z", "event": "step_fail", "step": "kas_build", "exit_code": 2, **extra}


RUN_END_FAILED = {"ts": "2026-10-19T12:00:06Z", "event": "run_end", "exit_code": 2}


def write_stop_request(env: dict[str, str], run_dir: Path, *, force: bool = False) -> None:
    out = py(
        env,
        "import sys; from pathlib import Path; from bakar import observability as o;"
        "print(o.write_stop_request(Path(sys.argv[1]), force=sys.argv[2] == '1'))",
        str(run_dir),
        "1" if force else "0",
    )
    if out.strip() != "True":
        raise RuntimeError(f"write_stop_request returned {out.strip()!r}")


def json_out(proc: subprocess.CompletedProcess[str]) -> dict | None:
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def describe(proc: subprocess.CompletedProcess[str]) -> str:
    return f"rc={proc.returncode} stdout={proc.stdout.strip()[:300]!r} stderr={proc.stderr.strip()[-300:]!r}"


def status_failure(env: dict[str, str], ws: Path, run_id: str) -> tuple[dict | None, subprocess.CompletedProcess[str]]:
    proc = bakar(env, ws, "status", run_id, "--json")
    return json_out(proc), proc


def main() -> int:
    smoke_lib = _load_smoke_lib()
    manual = [smoke_lib.ManualStep("human", text) for text in MANUAL_STEPS_TEXT]
    with (
        smoke_lib.SmokeRun(expected=9, manual_steps=manual) as smoke,
        smoke_lib.isolated_env("bakar-smoke-frr-") as (root, env),
    ):
        home = root / "home"
        runtime = root / "runtime"
        home.mkdir()
        runtime.mkdir(mode=0o700)
        env.update(
            {
                "HOME": str(home),
                "XDG_RUNTIME_DIR": str(runtime),
                "PYTHONPATH": str(SRC),
                "NO_COLOR": "1",
                "TERM": "dumb",
                "COLUMNS": "200",
            }
        )
        for var in [v for v in env if v.startswith("BAKAR_")]:
            del env[var]
        ws = root / "ws"
        ws.mkdir()
        (ws / ".bakar.toml").write_text("")
        (ws / "kas.yml").write_text(KAS_YAML)

        # The subprocesses must import this worktree's code, not the main checkout's.
        origin = py(env, "import bakar; print(bakar.__file__)").strip()
        smoke.check(
            "bakar subprocesses import this worktree's src",
            Path(origin).resolve().is_relative_to(SRC.resolve()),
            origin,
        )

        # (a) a stopped failed run reads as stopped in status, wait and triage.
        rid = RUN_IDS["a"]
        run_a = seed_run(ws, rid, step_fail(reason="exit_code=2", error=ERROR_TEXT), RUN_END_FAILED)
        write_stop_request(env, run_a)
        doc, st = status_failure(env, ws, rid)
        wt = bakar(env, ws, "wait", rid, "--timeout", "5")
        tr = bakar(env, ws, "triage", rid, "--json")
        tdoc = json_out(tr)
        problems = []
        if doc is None or doc.get("failure") is None:
            problems.append(f"status: {describe(st)}")
        elif (doc["failure"].get("step"), doc["failure"].get("error")) != (STOPPED_STEP, STOPPED_ERROR):
            problems.append(f"status failure={doc['failure']}")
        if wt.returncode != 1:
            problems.append(f"wait: {describe(wt)}")
        if tdoc is None or (tdoc.get("failing_step"), tdoc.get("fail_reason")) != (STOPPED_STEP, STOPPED_ERROR):
            problems.append(f"triage: {describe(tr)}")
        smoke.check("(a) stopped run: status stopped, wait exits 1, triage stopped", not problems, "; ".join(problems))

        # (b) a zero exit with a stop request stays succeeded.
        rid = RUN_IDS["b"]
        run_b = seed_run(ws, rid, {"ts": "2026-10-19T12:00:06Z", "event": "run_end", "exit_code": 0})
        write_stop_request(env, run_b)
        doc, st = status_failure(env, ws, rid)
        smoke.check(
            "(b) zero exit with stop request: state succeeded, failure null",
            doc is not None and doc.get("state") == "succeeded" and doc.get("failure") is None,
            describe(st),
        )

        # (c) no stop request: the step's captured error is the reason.
        rid = RUN_IDS["c"]
        seed_run(ws, rid, step_fail(reason="exit_code=2", error=ERROR_TEXT), RUN_END_FAILED)
        doc, st = status_failure(env, ws, rid)
        failure = (doc or {}).get("failure") or {}
        smoke.check(
            "(c) failed run without stop request reports the captured error",
            failure.get("step") == "kas_build" and failure.get("error") == ERROR_TEXT,
            describe(st),
        )

        # (d) a long coloured error line comes back bounded and clean.
        # Substitution: the 200-character bound and the control-character removal
        # apply where the error line is captured (BuildUIState.process_line ->
        # log_text.error_line), and status reports the recorded text verbatim.
        # The 400-character escape-laden line is therefore fed through the real
        # capture path in a subprocess and its result is what the seeded
        # step_fail event carries, as kas_build records it, rather than writing
        # the raw line into events.jsonl.
        raw_line = "\x1b[1;31mERROR: \x1b[0m" + ("x\x1b[32my\x1b[0m\x07" * 70) + "z" * 50
        captured = json.loads(
            py(
                env,
                "import sys, json; from bakar.steps.build_ui import BuildUIState;"
                "ui = BuildUIState(); ui.process_line(sys.argv[1]); print(json.dumps(ui.first_error))",
                raw_line,
            )
        )
        rid = RUN_IDS["d"]
        seed_run(ws, rid, step_fail(reason="exit_code=2", error=captured), RUN_END_FAILED)
        doc, st = status_failure(env, ws, rid)
        err = ((doc or {}).get("failure") or {}).get("error")
        ok_d = (
            isinstance(err, str)
            and len(raw_line) >= 400
            and len(err) <= 200
            and err.startswith("ERROR: ")
            and not any(ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F for ch in err)
        )
        smoke.check(
            "(d) 400-char coloured error line reads back bounded to 200 clean characters",
            ok_d,
            f"raw_len={len(raw_line)} error={err!r} {describe(st)}",
        )

        # (e) a structured task failure wins over the stop request.
        rid = RUN_IDS["e"]
        run_e = seed_run(ws, rid, step_fail(reason="exit_code=2"), RUN_END_FAILED)
        (run_e / "bitbake-events.json").write_text(
            json.dumps({"failures": [{"recipe": "busybox", "task": "do_compile", "logfile": None}]})
        )
        write_stop_request(env, run_e)
        doc, st = status_failure(env, ws, rid)
        tr = bakar(env, ws, "triage", rid, "--json")
        tdoc = json_out(tr)
        failure = (doc or {}).get("failure") or {}
        problems = []
        if (failure.get("recipe"), failure.get("task"), failure.get("step")) != ("busybox", "do_compile", None):
            problems.append(f"status: {describe(st)}")
        if tdoc is None or tdoc.get("fail_reason") is not None or tdoc.get("failing_step") != "busybox:do_compile":
            problems.append(f"triage: {describe(tr)}")
        smoke.check(
            "(e) structured task failure beats the stop request in status and triage", not problems, "; ".join(problems)
        )

        # (f) an ERROR line written to kas.log after the run ended is never read.
        rid = RUN_IDS["f"]
        run_f = seed_run(ws, rid, step_fail(reason="exit_code=2"), RUN_END_FAILED)
        with (run_f / "kas.log").open("a") as fh:
            fh.write("ERROR: appeared after the run ended\n")
        doc, st = status_failure(env, ws, rid)
        failure = (doc or {}).get("failure") or {}
        smoke.check(
            "(f) late kas.log ERROR line leaves failure.error at exit_code=2",
            failure.get("step") == "kas_build" and failure.get("error") == "exit_code=2",
            describe(st),
        )

        # (g) an unknown run id: triage exits 1 and prints nothing on stdout.
        tr = bakar(env, ws, "triage", "no-such-run", "--json")
        smoke.check(
            "(g) triage no-such-run --json exits 1 with empty stdout",
            tr.returncode == 1 and tr.stdout == "",
            describe(tr),
        )

        # (h) bakar stop writes the record only when a signal to a live build follows.
        smoke.check("(h) stop records a request only for a live build", *stop_scenario(env, ws))

        return smoke_lib.finish(smoke)


def _dead_pid(env: dict[str, str]) -> int:
    proc = subprocess.Popen(["true"], env=env, stdin=subprocess.DEVNULL)
    proc.wait()
    return proc.pid


def stop_scenario(env: dict[str, str], ws: Path) -> tuple[bool, str]:
    launch = (
        "import sys; from pathlib import Path; from bakar import build_stop;"
        "build_stop.write_launch_record(Path(sys.argv[1]), pgid=int(sys.argv[2]), mode='host')"
    )
    problems: list[str] = []

    # Part 1: the recorded wrapper is gone, so no signal follows and no record is written.
    rid_dead = RUN_IDS["h"] + "dead"
    run_dead = seed_run(ws, rid_dead)
    py(env, launch, str(run_dead), str(_dead_pid(env)))
    first = bakar(env, ws, "stop", "kas.yml")
    if (run_dead / "stop-request.json").exists():
        problems.append(f"no-live stop wrote a record: {describe(first)}")

    # Part 2: a live stand-in. Its argv[0] is kas-container so bakar's cmdline
    # check accepts it; setsid puts it in its own group so the force ladder
    # (SIGTERM -> SIGKILL on that group) can only reach it. It is started
    # through a shell that exits at once, so it is reparented to a reaper:
    # were this script its parent, the killed stand-in would linger as a
    # zombie and bakar would correctly report its group as still alive.
    rid_live = RUN_IDS["h"] + "live"
    run_live = seed_run(ws, rid_live)
    pid = int(
        run(
            ["bash", "-c", "setsid bash -c 'exec -a kas-container sleep 300' </dev/null >/dev/null 2>&1 & echo $!"],
            env,
            ws,
        ).stdout.strip()
    )
    try:
        if not _await_cmdline(pid, b"kas-container"):
            return False, f"stand-in pid {pid} never showed kas-container in its cmdline"
        py(env, launch, str(run_live), str(pid))
        second = bakar(env, ws, "stop", "kas.yml", "--run", rid_live, "--force")
        try:
            record = json.loads((run_live / "stop-request.json").read_text())
        except (OSError, ValueError) as exc:
            record = None
            problems.append(f"force stop record unreadable ({exc}): {describe(second)}")
        if record is not None and (not isinstance(record, dict) or record.get("force") is not True):
            problems.append(f"force stop record={record!r}")
        leftovers = sorted(p.name for p in run_live.iterdir() if p.name.endswith(".tmp"))
        if leftovers:
            problems.append(f"temporary files left: {leftovers}")
        if _group_alive(pid):
            problems.append(f"stand-in still alive after force stop: {describe(second)}")
        if second.returncode != 0:
            problems.append(f"force stop exit: {describe(second)}")
    finally:
        if _group_alive(pid):
            os.killpg(pid, signal.SIGKILL)
    return not problems, "; ".join(problems)


def _await_cmdline(pid: int, token: bytes, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if token in Path(f"/proc/{pid}/cmdline").read_bytes():
                return True
        except OSError:
            return False
        time.sleep(0.05)
    return False


def _group_alive(pgid: int, timeout: float = 10.0) -> bool:
    """True when process group ``pgid`` still exists after waiting up to ``timeout`` for it to go."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return False
        if time.monotonic() >= deadline:
            return True
        time.sleep(0.1)


if __name__ == "__main__":
    sys.exit(main())
