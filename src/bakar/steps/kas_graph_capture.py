"""Build-time dependency-graph capture, split out of :mod:`bakar.steps.kas_build`
(task 10.2).

Every symbol here is re-exported at ``bakar.steps.kas_build``, and
``tests/test_graph_capture.py`` monkeypatches the cluster's collaborators
(``_lock_holder_has_activity``, ``run_kas_subcommand``, ``run_shell_capture``)
on the ``kas_build`` module rather than on this one. A bare call to any of
those names from inside this module would resolve against THIS module's own
globals and never see such a patch, so every call to a collaborator that
lives on (or is re-exported at) ``kas_build`` goes through a function-body
deferred ``from bakar.steps import kas_build`` and a ``kas_build.<name>(...)``
call instead of a bare name - including calls between functions defined here,
since the patch target is always ``kas_build``, never this module.
"""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from bakar.observability import RunLogger
    from bakar.steps.kas_build import KasBuildContext

#: Filename of the sidecar recording what a captured graph describes.
GRAPH_MARKER_NAME = "dependency-graph.json"

#: The two artifacts ``bitbake -g`` emits into TOPDIR.
GRAPH_ARTIFACTS = ("task-depends.dot", "pn-buildlist")


def graph_capture_command(target: str) -> str:
    """Return the payload that emits a dependency graph and releases the lock.

    ``bitbake -g`` starts a cooker server and leaves it running, holding
    ``build/bitbake.lock``. Without the ``bitbake -m`` that follows, the next
    bitbake invocation against this build directory is refused outright with
    "Only one copy of bitbake should be run against a build directory".

    Sequenced with ``;`` and an explicit ``rc``, never ``&&``. The failure case
    is precisely the one that most needs the unlock: a ``bitbake -g`` that
    starts its cooker and then exits non-zero - a parse error, ENOSPC, a recipe
    broken by whatever is being built - would short-circuit an ``&&`` and leave
    that server holding the lock. Nothing about the build that stranded it
    fails; the NEXT build fails, on someone else's machine, naming neither this
    capture nor the run that caused it.

    This same ``bitbake -m`` also costs the next build its warm cooker on a
    capture SUCCESS, not only on failure: it kills the idle cooker that
    :func:`_wait_for_cooker_idle` waited for, so the next invocation against
    this build directory re-parses cold instead of reconnecting. That cost is
    accepted deliberately rather than engineered around. Whether ``bitbake -g``
    would actually reconnect to the build's own cooker or spawn a fresh one was
    never empirically measured, and the two failure modes this choice trades
    between are asymmetric: keeping the kill costs a slower next build, which
    is bounded and self-correcting. Dropping it risks stranding a live-owner
    lock that :func:`clear_stale_bitbake_locks` will not clear - it only clears
    locks whose owner has crashed, and refuses a peer-held lock on a shared NFS
    TOPDIR outright even when idle - a failure that could land on a different
    build entirely. Anyone who values the warm-reconnect path over the graph
    artifact has ``--no-capture-graph`` as the escape hatch.
    """
    return f"bitbake -g {shlex.quote(target)}; rc=$?; bitbake -m; exit $rc"


#: How long to wait for the build's own cooker to go idle before capturing.
#: Measured on a real bench run: the capture was refused at 17:07:07 and the
#: identical command succeeded at 17:07:17, so ten seconds is the observed
#: figure and this is a generous multiple of it.
GRAPH_CAPTURE_IDLE_TIMEOUT_S = 60.0

#: Interval between idle probes. Each probe scans the lock holder's process
#: tree, so this is not free enough to spin on.
GRAPH_CAPTURE_POLL_S = 2.0

#: Deadline for the capture itself, once a quiet cooker has been obtained.
#: A full metadata parse on a large tree runs for minutes, so this is sized to
#: catch a hang rather than to cap normal work - an order of magnitude above
#: the idle wait above, which bounds a different thing entirely.
GRAPH_CAPTURE_TIMEOUT_S = 900.0

#: Deadline for resolving the build target via ``kas dump``, before the
#: cooker-idle wait or the capture itself even start. ``kas dump`` only
#: resolves YAML includes and overlays - it never runs bitbake - so this is
#: sized like the idle wait above rather than the capture's own budget: a
#: hang here costs a wasted wait, not a stranded lock, but the spec still
#: requires the whole capture - target resolution included - to complete or
#: be abandoned within a bounded interval rather than block a finished build
#: indefinitely.
GRAPH_CAPTURE_TARGET_RESOLVE_TIMEOUT_S = 60.0


def _wait_for_cooker_idle(
    build_dir: Path,
    log: RunLogger,
    *,
    timeout: float = GRAPH_CAPTURE_IDLE_TIMEOUT_S,
    poll: float = GRAPH_CAPTURE_POLL_S,
) -> bool:
    """Wait until the build's bitbake cooker has no activity left, or time out.

    The lock is never released for us to take - that is the thing to understand
    here. bitbake's cookerdaemon persists after a build so the next invocation
    reconnects instead of respawning, which is a documented happy path - but
    only for a build that does NOT run this capture, or one where the capture
    was declined via ``--no-capture-graph``. What changes is that the holder
    goes from having worker/client ACTIVITY to being a bare idle server, and
    :func:`clear_stale_bitbake_locks` already treats those two states
    differently: the first refuses, the second is left alone precisely so a
    following invocation can reconnect - for a build that stops here.

    A build that goes on to capture does not get that reconnect. The
    ``bitbake -m`` in :func:`graph_capture_command` kills the cooker this
    function just waited to go idle, so the NEXT invocation against this build
    directory re-parses cold instead of reconnecting. That cost is accepted
    deliberately; see the comment on :func:`graph_capture_command` for why.

    So this waits on the same predicate that would refuse us, rather than
    sleeping a guessed interval and hoping.

    Returns False on timeout rather than raising. A capture that could not get
    a quiet cooker is an absent optional artifact, and blocking a completed
    build's teardown on one would be a worse trade than going without it.
    """
    from bakar.steps import kas_build

    deadline = time.monotonic() + timeout
    while True:
        if not kas_build._lock_holder_has_activity(build_dir):
            return True
        if time.monotonic() >= deadline:
            log.warn(
                f"dependency graph: the build's bitbake cooker was still active after {timeout:.0f}s; "
                "skipping capture rather than delaying teardown further"
            )
            return False
        time.sleep(poll)


def _resolve_capture_target(ctx: KasBuildContext, log: RunLogger) -> str | None:
    """Resolve the bitbake target this build produced.

    Neither obvious candidate works, and both were tried against a real build.
    ``cfg.image`` resolves to ``generic`` on a meta-avocado workspace, and
    ``ctx.target`` is None whenever the kas configuration supplies the target
    rather than the command line - which is the ordinary case.

    ``kas dump`` is the authoritative answer because it resolves includes and
    overlays, and a layered configuration's effective ``target:`` can come from
    any file in the stack. Reading the top-level YAML directly would get the
    common case right and the layered one silently wrong.
    """
    from bakar.steps import kas_build

    if ctx.target:
        return ctx.target
    with tempfile.NamedTemporaryFile(suffix=".yml", delete=False) as fh:
        dump_path = Path(fh.name)
    try:
        rc = kas_build.run_kas_subcommand(
            ctx,
            "dump",
            [],
            step="graph_capture_kas_dump",
            capture_to=dump_path,
            timeout=GRAPH_CAPTURE_TARGET_RESOLVE_TIMEOUT_S,
        )
        if rc != 0:
            log.warn(f"dependency graph: kas dump exited {rc}; cannot resolve the build target")
            return None
        resolved = yaml.safe_load(dump_path.read_text())
    except Exception as exc:  # noqa: BLE001 - target resolution must not crash a completed build
        log.warn(f"dependency graph: could not resolve the build target ({exc})")
        return None
    finally:
        dump_path.unlink(missing_ok=True)
    if not isinstance(resolved, dict):
        return None
    target = resolved.get("target")
    # kas allows a list of targets; graph the first, and say so rather than
    # silently graphing one of several as though it were the whole build.
    if isinstance(target, list):
        if not target:
            return None
        if len(target) > 1:
            log.info(f"dependency graph: configuration builds {len(target)} targets; graphing {target[0]}")
        target = target[0]
    return target if isinstance(target, str) and target else None


def _capture_dependency_graph(ctx: KasBuildContext, log: RunLogger) -> dict[str, str] | None:
    """Emit the dependency graph for the build just completed into the run dir.

    Runs AFTER the build's terminal step event. That placement cannot distort
    the reported durations even in principle, because bakar derives them from
    bitbake's own task event timestamps rather than from a wall clock the
    harness holds around the build - unlike the reference implementation this
    ports, where the capture had to be sequenced outside a timed region.

    Never raises: a build that produced an image and no graph is a successful
    build missing an optional analysis artifact, and an exception escaping here
    would turn that into a crash after the work was already done.

    Returns immediately when ``cfg.capture_graph`` is off (`[build] capture_graph
    = false` / `bakar build --no-capture-graph`). The early return sits ahead of
    the cooker-idle wait deliberately: that wait is up to 60s of pure waiting,
    and declining the capture has to cost nothing.
    """
    from bakar.steps import kas_build

    cfg = ctx.cfg
    if not cfg.capture_graph:
        log.info("dependency graph: capture declined (capture_graph off); skipping")
        return None
    # captured is declared before the try so every except clause below -
    # including a KeyboardInterrupt or unexpected error during the copy loop
    # itself - can clean up whatever was already copied, not just the two
    # branches that used to handle it inline.
    captured: dict[str, str] = {}
    try:
        # Target resolution (a bounded `kas dump`) and the cooker-idle wait
        # (up to 60s of polling) both sit inside this try now, not before it:
        # a Ctrl-C during either used to escape this function's "never raises"
        # contract entirely, since the KeyboardInterrupt handler below could
        # only catch what happened after this point.
        target = kas_build._resolve_capture_target(ctx, log)
        if not target:
            log.warn("dependency graph: no build target resolved; skipping capture")
            return None
        # The build's own cooker still holds the lock at this point, with activity.
        # Without this wait every capture is refused before it starts - which is
        # what the first real build did, on every attempt.
        if not kas_build._wait_for_cooker_idle(cfg.bsp_root / cfg.build_dir_name, log):
            return None
        # Recorded before the capture runs so a source artifact can be checked
        # against it afterward: mtimes are wall-clock, so this must be too.
        # A `bitbake -g` that exits 0 without rewriting its outputs (e.g. a
        # metadata-parse short-circuit) would otherwise let a previous build's
        # graph get copied out and stamped with this run's provenance marker -
        # exactly what the marker exists to prevent.
        capture_started_at = time.time()
        # SHELL is pinned because kas hands the -c payload to $SHELL rather than
        # choosing a shell. The login shell here is fish, which rejects the
        # `rc=$?` idiom above with "Unsupported use of '='" at exit 127 - before
        # bitbake starts, so the traceback names bitbake and not the shell.
        rc = kas_build.run_shell_capture(
            ctx,
            graph_capture_command(target),
            log.run_dir / "depgraph.log",
            step="graph_capture",
            env_overrides={"SHELL": "/bin/bash"},
            timeout=GRAPH_CAPTURE_TIMEOUT_S,
            isolate_process_group=True,
        )
        if rc != 0:
            log.warn(f"dependency graph: capture exited {rc}; see {log.run_dir / 'depgraph.log'}")
            return None

        # Not cfg.resolved_tmpdir.parent: with a node-local tmpdir override
        # configured (BuildConfig.local_tmpdir_base, host mode), resolved_tmpdir
        # points under that override base, not under TOPDIR - its parent would
        # be the override base itself, not the build directory bitbake -g
        # actually writes the two artifacts into.
        topdir = cfg.bsp_root / cfg.build_dir_name
        for name in GRAPH_ARTIFACTS:
            src = topdir / name
            if not src.is_file():
                log.warn(f"dependency graph: {name} not produced at {src}")
                return None
            src_mtime = src.stat().st_mtime
            if src_mtime < capture_started_at:
                log.warn(
                    f"dependency graph: {name} at {src} predates this capture "
                    f"(mtime {src_mtime:.0f} < start {capture_started_at:.0f}); "
                    "skipping rather than publishing a stale graph under this run's marker"
                )
                return None
            dest = log.run_dir / name
            shutil.copy2(src, dest)
            captured[name] = str(dest)

        # The sidecar is what makes provenance checkable. Co-location alone is
        # what lets a graph left behind by a previous build read as this run's.
        marker = {"target": target, "captured_at": time.time(), "artifacts": captured}
        (log.run_dir / GRAPH_MARKER_NAME).write_text(json.dumps(marker, indent=2))
        log.info(f"dependency graph: captured for {target}")
    except KeyboardInterrupt:
        # Listed FIRST and separately because KeyboardInterrupt derives from
        # BaseException, not Exception, so the clause below would never catch
        # it. Without this a Ctrl-C during the capture escapes and discards a
        # completed build's reporting for a traceback.
        log.warn("dependency graph: interrupted during capture")
        return None
    except subprocess.TimeoutExpired:
        # Named ahead of the blanket clause below so the log says the capture
        # hung rather than that it "failed", and so the distinction survives
        # into the run log a later triage reads.
        log.warn(
            f"dependency graph: capture exceeded {GRAPH_CAPTURE_TIMEOUT_S:.0f}s and was killed; "
            f"see {log.run_dir / 'depgraph.log'}"
        )
        return None
    except Exception as exc:  # noqa: BLE001 - a completed build must not crash on capture failure
        log.warn(f"dependency graph: capture failed ({exc})")
        return None
    finally:
        # Every early return above happens before the marker is written, so a
        # marker's absence is what "capture did not complete" means here: any
        # artifact already copied at that point is a partial, unexplained
        # capture and must not be left behind. A completed capture leaves
        # `captured` in `finally` too, but the marker already exists by then,
        # so the check below is a no-op on the success path.
        if not (log.run_dir / GRAPH_MARKER_NAME).exists():
            for copied in captured.values():
                Path(copied).unlink(missing_ok=True)
    return captured
