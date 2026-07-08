"""Run ``bitbake -p`` repeatedly to stress-test the parser fork-race fix.

The bitbake parser fork-race is probabilistic: a single passing parse
is not evidence the override holds. This step loops parse-only N times
inside ``kas-container``, captures each iteration's stdout+stderr to
its own log file, and scans for any of the canonical fork-race
signatures from :mod:`bakar.fork_race_signatures`.

The aggregate result lands in ``summary.json`` next to the per-run
logs; both files live under
``<bsp>/build/runs/<run-id>/stress-parse/``. The CLI consumer in
:mod:`bakar.cli` exits non-zero when any iteration tripped a
signature.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
import time
from typing import TYPE_CHECKING

from bakar.fork_race_signatures import FORK_RACE_SIGNATURES
from bakar.steps import bitbake_override as step_override
from bakar.steps import kas_build as step_kas
from bakar.steps.kas_build import KasBuildContext

if TYPE_CHECKING:
    from pathlib import Path

    from bakar.bsp_model import BspModel
    from bakar.config import BuildConfig
    from bakar.observability import RunLogger


def _clear_parse_cache(cfg: BuildConfig, log: RunLogger, iteration: int) -> bool:
    """Remove parse caches so the next ``bitbake -p`` reparses from scratch.

    The fork-in-multi-threaded race fires when bitbake-server forks parser
    workers to chew through .bb files. With a warm parse cache (the
    ``Loaded N entries from dependency cache`` path) almost every recipe
    is a cache hit and the fork pool barely spins up - which means a
    cached iteration is not exercising the race at all.

    Bitbake writes parse cache state to two locations and we have to wipe
    both, otherwise the dep-cache loaded count creeps up across iterations
    and the race window narrows after iter 4 or so:

    * ``<bsp>/build/cache/`` - the codeparser/unihash cache directory
      that older bitbake versions point ``${CACHE}`` at by default.
      Holds ``bb_codeparser.dat``, ``bb_unihashes.dat``, and friends.
    * ``<bsp>/build/tmp/cache/`` - where ``${TMPDIR}/cache/`` resolves on
      this layout, holding the per-multiconfig ``bb_cache.dat.<hash>``
      files. Walnascar bitbake repopulates this on every parse, and
      missing it was the reason the original run saw the race
      fire 3/3 in iter 1-3 (cold-cache) but 0/7 in iter 4-10 (cache
      slowly recovered into ``tmp/cache/`` even though ``build/cache/``
      was wiped each iter).

    Hashserv reseeds itself on the next start; that is fine for
    parse-only stress runs.

    Returns ``True`` if at least one cache directory was actually removed,
    ``False`` if there was nothing there (first iteration on a fresh
    workspace).
    """
    targets = [
        cfg.bsp_root / "build" / "cache",
        cfg.bsp_root / "build" / "tmp" / "cache",
    ]
    cleared_any = False
    for cache_dir in targets:
        if not cache_dir.is_dir():
            continue
        shutil.rmtree(cache_dir, ignore_errors=True)
        log.info(f"stress-parse[{iteration:02d}]: cleared {cache_dir}")
        cleared_any = True
    return cleared_any


def _clear_bitbake_runtime(cfg: BuildConfig, log: RunLogger, iteration: int) -> bool:
    """Force a fresh bitbake-server and fresh bytecode load per iteration.

    Two persistence pitfalls bite stress-parse runs that change the
    ``upstream-bitbake`` source mid-experiment (probes, fix attempts):

    * **Long-running cookerdaemon**: ``bitbake -p`` reconnects to an
      existing daemon if ``<bsp>/build/bitbake.lock`` claims one is
      alive. That daemon imported ``bb.cooker`` (and friends) at its
      own startup; the in-memory bytecode is frozen for its lifetime,
      so source edits made AFTER the daemon spawned never take effect
      until the daemon exits. Wiping the lock + the unix socket forces
      the next iteration's ``bitbake -p`` to spawn a brand-new daemon,
      which re-imports every bb module from disk.
    * **Stale ``__pycache__``**: Python caches compiled bytecode under
      each package's ``__pycache__/`` and skips recompilation when the
      cached ``.pyc`` already matches the source's mtime. Editor saves
      and ``git`` operations both update mtime correctly so this is
      usually fine, but there is no harm in clearing the cache
      unconditionally to remove any residual ambiguity. Inside
      ``<bsp>/upstream-bitbake/`` the cache dirs are pure build
      artifact - safe to wipe.

    Returns ``True`` if anything was actually removed, ``False`` if the
    workspace was already clean (typical first-iteration state).
    """
    cleared_any = False

    for runtime_path in (
        cfg.bsp_root / "build" / "bitbake.lock",
        cfg.bsp_root / "build" / "bitbake.sock",
    ):
        if runtime_path.exists() or runtime_path.is_symlink():
            try:
                runtime_path.unlink()
            except OSError:
                continue
            log.info(f"stress-parse[{iteration:02d}]: removed {runtime_path}")
            cleared_any = True

    override_root = cfg.bsp_root / "upstream-bitbake"
    if override_root.is_dir():
        pycache_count = 0
        for pycache in override_root.rglob("__pycache__"):
            if pycache.is_dir():
                shutil.rmtree(pycache, ignore_errors=True)
                pycache_count += 1
        if pycache_count:
            log.info(f"stress-parse[{iteration:02d}]: cleared {pycache_count} __pycache__ dirs under {override_root}")
            cleared_any = True

    return cleared_any


def _scan_log(log_path: Path) -> list[dict[str, str]]:
    """Return every (pattern, matched-line) hit in ``log_path``.

    Reads the file with ``errors="replace"`` so a stray binary byte
    from a torn parser worker does not crash the post-mortem scan.
    """
    if not log_path.is_file():
        return []
    text = log_path.read_text(errors="replace")
    hits: list[dict[str, str]] = [
        {"pattern": pattern.pattern, "match": line}
        for line in text.splitlines()
        for pattern in FORK_RACE_SIGNATURES
        if pattern.search(line)
    ]
    return hits


def _build_command(
    target: str,
    parse_threads: int | None,
    *,
    host_mode: bool = False,
    postfile: str | None = None,
    python_executable: Path | None = None,
) -> str:
    """Compose the in-container bitbake invocation.

    ``--parse-threads`` is rendered as a leading ``BB_NUMBER_PARSE_THREADS=N``
    env assignment on the bash command line so it overrides whatever the
    kas YAML or the host shell exported. Bitbake honours this knob
    directly during the parse phase.

    In host mode, prepend ``BB_PYTHON3=<bakar-interpreter>`` so bitbake's
    bin/bitbake re-execs into the same Python bakar was installed under.
    kas filters environment passthrough at ``kas/context.py``, so a host
    env var would not survive into the kas shell - inlining the
    assignment on the command line goes through verbatim. When
    ``python_executable`` is given, BB_PYTHON3 points at it instead -
    used to run a patched obmalloc CPython
    without reinstalling bakar under it.

    When ``postfile`` is given, append ``-R <path>`` so bitbake reads it
    after the standard config files. Used in host mode to apply
    ``INHERIT:remove = "sanity"`` and skip OE-core's host-distro sanity
    checks, which fail on Arch (perl modules, gcc version, etc.) before
    parsing can begin and therefore mask the parser-fork-race we are
    actually probing.
    """
    cmd = f"bitbake -p {target}"
    if postfile is not None:
        cmd = f"bitbake -R {postfile} -p {target}"
    if parse_threads is not None:
        cmd = f"BB_NUMBER_PARSE_THREADS={parse_threads} {cmd}"
    if host_mode:
        bb_python = str(python_executable) if python_executable is not None else sys.executable
        cmd = f"BB_PYTHON3={bb_python} {cmd}"
    # PYTHONMALLOC=malloc is the round 11 Phase 2 finding:
    # routes Python allocations through libc malloc instead of obmalloc,
    # eliminating the obmalloc/glibc-arena-at-fork interaction that
    # produces the dominant SIGABRT crash mode on Python 3.14 GIL.
    # Drops the 3.14.4 GIL host-mode race rate from ~50% to ~5%.
    # Allow user override (PYTHONMALLOC=mimalloc on 3.13+ matches but is
    # version-gated; LD_PRELOAD libmimalloc is worse per the same probe).
    forwarded = [("PYTHONMALLOC", os.environ.get("PYTHONMALLOC", "malloc"))]
    for name in ("BB_FORK_DIAG", "MALLOC_ARENA_MAX", "LD_PRELOAD"):
        val = os.environ.get(name)
        if val:
            forwarded.append((name, val))
    prefix_assigns = " ".join(f"{n}={shlex.quote(v)}" for n, v in forwarded)
    passthrough = " ".join(n for n, _ in forwarded)
    cmd = f"BB_ENV_PASSTHROUGH_ADDITIONS='{passthrough}' {prefix_assigns} {cmd}"
    return cmd


def _write_host_mode_postfile(out_dir: Path) -> Path:
    """Write a postfile that disables OE-core's host-distro sanity check.

    Returns the absolute path. The postfile is written once per
    stress-parse run and reused across iterations.
    """
    postfile = out_dir / "host-mode.conf"
    postfile.write_text(
        # Skip OE-core's host-distro sanity check on Arch. The check
        # bb.fatals on missing perl modules, gcc version mismatches,
        # and host-distro tool checks that are immaterial to a parse-
        # only probe. Without this skip the daemon hits BBHandledException
        # before parse begins and masks the fork-race we are probing.
        'INHERIT:remove = "sanity"\n'
    )
    return postfile


def _override_payload(cfg: BuildConfig) -> dict[str, str | None]:
    """Snapshot the override status for ``summary.json["override"]``."""
    snap = step_override.status(cfg)
    return {
        "state": snap.state,
        "branch": snap.branch,
        "sha": snap.sha,
        "upstream_version": snap.upstream_version,
        "bsp_version": snap.bsp_version,
    }


def _env_payload(parse_threads: int | None) -> dict[str, str]:
    """Sample env knobs that affect the race for forensic posterity."""
    keys = ("BB_NUMBER_PARSE_THREADS", "BB_NUMBER_THREADS", "BB_HASHSERVE", "NPROC")
    payload = {k: os.environ.get(k, "") for k in keys}
    if parse_threads is not None:
        payload["BB_NUMBER_PARSE_THREADS"] = str(parse_threads)
    return payload


def run(
    cfg: BuildConfig,
    log: RunLogger,
    *,
    bsp: BspModel,
    overlay_source: Path,
    runs: int,
    target: str,
    parse_threads: int | None,
    label: str | None = None,
    python_executable: Path | None = None,
) -> dict:
    """Execute ``runs`` parse-only iterations and return the summary dict.

    Side effects: writes ``run-NN.log`` per iteration and a final
    ``summary.json`` under
    ``<run_dir>/stress-parse/``. Emits one ``step_start``/``step_ok``
    (or ``step_fail``) event per iteration via ``log``.

    ``overlay_source`` is the path to the static tuning overlay; passed
    through to :func:`bakar.steps.kas_build.run_shell_capture`
    on every iteration so each parse-only run carries the same tuning
    block as a real build.

    ``label`` tags the resulting ``summary.json`` so a multi-run sweep
    (e.g. patch-ablation matrix or CPython version sweep) can be
    aggregated by label downstream. The harness records it verbatim;
    aggregators are responsible for choosing a labelling scheme.

    ``python_executable`` overrides which Python bitbake re-execs into
    (via BB_PYTHON3) and which interpreter's bin/ leads PATH inside the
    kas-shell. Used to validate a patched obmalloc CPython
    without reinstalling bakar under that interpreter. Recorded in
    ``summary.json["python_executable"]`` for the audit trail.
    """
    out_dir = log.run_dir / "stress-parse"
    out_dir.mkdir(parents=True, exist_ok=True)

    postfile = str(_write_host_mode_postfile(out_dir)) if cfg.host_mode else None
    command = _build_command(
        target,
        parse_threads,
        host_mode=cfg.host_mode,
        postfile=postfile,
        python_executable=python_executable,
    )
    summary: dict = {
        "bsp_family": cfg.bsp_family,
        "manifest": cfg.manifest,
        "machine": cfg.machine,
        "image": cfg.image,
        "target": target,
        "runs": runs,
        "passed": 0,
        "failed": 0,
        "errored": 0,
        "elapsed_seconds": [],
        "exit_codes": [],
        "cache_cleared_pre_iter": [],
        "runtime_cleared_pre_iter": [],
        "override": _override_payload(cfg),
        "env": _env_payload(parse_threads),
        "failure_signatures": [],
    }
    if label is not None:
        summary["label"] = label
    if python_executable is not None:
        summary["python_executable"] = str(python_executable)

    for i in range(1, runs + 1):
        iter_log = out_dir / f"run-{i:02d}.log"
        cache_was_cleared = _clear_parse_cache(cfg, log, i)
        summary["cache_cleared_pre_iter"].append(cache_was_cleared)
        runtime_was_cleared = _clear_bitbake_runtime(cfg, log, i)
        summary["runtime_cleared_pre_iter"].append(runtime_was_cleared)
        log.step_start(
            "stress_parse_iter",
            iteration=i,
            of=runs,
            command=command,
            cache_cleared=cache_was_cleared,
            runtime_cleared=runtime_was_cleared,
        )
        t0 = time.monotonic()
        kas_ctx = KasBuildContext(cfg, log, cfg.kas_yaml, overlay_source)
        rc = step_kas.run_shell_capture(
            kas_ctx,
            command,
            iter_log,
            step=f"stress_parse_iter_{i:02d}",
            python_executable=python_executable,
        )
        elapsed = round(time.monotonic() - t0, 3)

        hits = _scan_log(iter_log)
        summary["elapsed_seconds"].append(elapsed)
        summary["exit_codes"].append(rc)

        if hits:
            summary["failed"] += 1
            for hit in hits:
                summary["failure_signatures"].append({"run": i, **hit})
            log.step_fail(
                "stress_parse_iter",
                reason="fork-race signature matched",
                iteration=i,
                exit_code=rc,
                elapsed=elapsed,
                matches=len(hits),
            )
        elif rc != 0:
            summary["errored"] += 1
            log.step_fail(
                "stress_parse_iter",
                reason="non-signature nonzero exit",
                iteration=i,
                exit_code=rc,
                elapsed=elapsed,
                matches=0,
            )
        else:
            summary["passed"] += 1
            log.step_ok(
                "stress_parse_iter",
                iteration=i,
                exit_code=rc,
                elapsed=elapsed,
                matches=0,
            )

    summary_path = out_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, default=str) + "\n")
    log.info(
        f"stress-parse: {summary['passed']}/{summary['runs']} passed "
        f"({summary['failed']} failed, {summary['errored']} errored), summary at {summary_path}"
    )
    return summary
