"""Regenerate the kas YAML and run `kas-container build`.

The YAML generator lives in :mod:`bspctl.kas`; this step wraps it
plus the build invocation with the measurement harness (``/usr/bin/time
-v`` plus a background ``du -sb build/tmp`` sampler), and layers in
the static tuning overlay (``overlays/bspctl-tuning-<bsp>.yml``)
on top of whatever kas YAML the caller passes in.

A pseudo-TTY is allocated for the kas-container subprocess so that
``kas-container``'s ``[ -t 1 ]`` check passes and it attaches ``-t -i`` on
the ``docker run`` call. That enables bitbake's knotty interactive UI
inside the container, which emits ``Currently N tasks running (X of Y
complete)`` status lines several times per second - a much livelier
counter than the per-task-start ``NOTE: Running task`` lines we get in
non-TTY mode. The PTY also means bitbake's stdout is line-flushed
rather than block-buffered, so ``varis log`` and the progress bar stay
responsive during long compile phases.
"""

from __future__ import annotations

import os
import pty
import re
import shutil
import signal
import subprocess
import sys
import sysconfig
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from rich.progress import (
    BarColumn,
    Progress,
    TextColumn,
    TimeElapsedColumn,
)

from bspctl.kas import KasGenOptions, write_yaml

if TYPE_CHECKING:
    from bspctl.bsp_model import BspModel
    from bspctl.config import BuildConfig
    from bspctl.observability import RunLogger

# Bitbake output patterns.  These drive the progress bar in run_build().
#
# CURRENT_RUNNING is the dominant signal during the execution phase when
# the PTY is active: bitbake's knotty UI reprints it several times per
# second.  RUNNING_TASK gives us a recipe label for the current task.
# PARSE_PROGRESS covers the parse phase before execution starts, and
# SETSCENE_RUNNING covers the setscene (sstate reuse) phase.
# SEVERITY_PASSTHROUGH is pulled out of the stream and printed above
# the bar so users see real problems without tailing kas.log.
CURRENT_RUNNING = re.compile(r"Currently \d+ tasks? running \((\d+) of (\d+) complete\)")
RUNNING_TASK = re.compile(r"NOTE: Running task (\d+) of (\d+) \(([^)]+)\)")
PARSE_PROGRESS = re.compile(r"Parsing recipes: (\d+)% \|[^|]*\| (\d+)/(\d+)")
SETSCENE_RUNNING = re.compile(r"Currently \d+ setscene tasks running \((\d+) of (\d+) complete\)")
SEVERITY_PASSTHROUGH = re.compile(r"\b(ERROR|FATAL|WARNING|QA Issue):")

# knotty in TTY mode emits ANSI CSI escapes to manipulate the cursor and
# redraw progress lines in place.  We strip both the standard CSI form
# (ESC [ ... letter) and the less common OSC form (ESC ] ... BEL) before
# writing to kas.log so downstream tools (triage, grep, varis log) see
# clean plain text.  The regex is deliberately conservative; anything
# exotic gets left as-is.
ANSI_CSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
ANSI_OSC_RE = re.compile(r"\x1b\][^\x07]*\x07")
LINE_SPLIT_RE = re.compile(rb"\r\n|\n|\r")

# Overlay materialization: the kas-container bind-mount only includes
# ``KAS_WORK_DIR`` (= bsp_root) as ``/work``. Symlinking the overlay
# under ``<bsp_root>/.varis/overlays/`` puts it inside that mount so
# the ``<user-yml>:<overlay>`` colon-joined arg resolves cleanly from
# the container's perspective.
_OVERLAY_DIR_RELPATH = Path(".varis") / "overlays"


def _strip_ansi(s: str) -> str:
    return ANSI_OSC_RE.sub("", ANSI_CSI_RE.sub("", s))


def _fmt_stall(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


def _fmt_du(delta: int) -> str:
    if delta <= 0:
        return "-"
    mb = delta / (1024 * 1024)
    if mb < 1024:
        return f"+{mb:.0f}M"
    return f"+{mb / 1024:.1f}G"


def materialize_overlay(cfg: BuildConfig, overlay_source: Path) -> Path:
    """Copy ``overlay_source`` into ``<bsp_root>/.varis/overlays/``.

    Returns the path *relative to* ``cfg.bsp_root`` so callers can
    pass it straight into the ``kas-container build <user>:<overlay>``
    colon-joined argument.

    Always overwrites the destination so the overlay content tracks
    ``overlay_source`` byte-for-byte on every invocation. Earlier
    revisions symlinked, but kas resolves symlinks before running its
    "all configs must share a git repo" check, so a YAML in repo A
    layered with a symlink whose target lives in repo B (the varis
    install) tripped ``All concatenated config files must belong to
    the same repository or all must be outside of versioning control``.
    Copying drops a real file into the user's tree, putting both
    configs in the same repo (or outside any repo) and sidesteps the
    bind-mount issue where a symlink target outside ``KAS_WORK_DIR``
    dangles inside the kas-container view.
    """
    overlay_dir = cfg.bsp_root / _OVERLAY_DIR_RELPATH
    overlay_dir.mkdir(parents=True, exist_ok=True)
    dest = overlay_dir / overlay_source.name
    if dest.is_symlink() or dest.is_file():
        dest.unlink()
    shutil.copy2(overlay_source, dest)
    return dest.relative_to(cfg.bsp_root)


def _resolve_user_yaml(cfg: BuildConfig, kas_yaml: Path) -> Path:
    """Return ``kas_yaml`` as a path relative to ``cfg.bsp_root``.

    kas-container's bind mount only covers ``KAS_WORK_DIR`` (=
    ``bsp_root``), so a YAML living outside that subtree cannot be
    read from inside the container. Reject those inputs with a clear
    error rather than letting kas-container fail with an opaque
    "config file not found" message.
    """
    abs_path = kas_yaml.resolve()
    try:
        return abs_path.relative_to(cfg.bsp_root)
    except ValueError as exc:
        raise RuntimeError(
            f"kas YAML {abs_path} is outside bsp_root {cfg.bsp_root}; "
            f"copy it under {cfg.bsp_root}/ (e.g. as {cfg.bsp_root}/my-build.yml) and re-run."
        ) from exc


def _ccache_args(cfg: BuildConfig) -> list[str]:
    """Return ``['--runtime-args', '-v host:/work/ccache:rw']`` for container builds.

    ``kas-container`` unconditionally resets ``KAS_RUNTIME_ARGS`` to its own
    defaults before its option-parsing loop, so injecting the flag via an env
    var is silently discarded.  The ``--runtime-args`` CLI flag (processed
    after the reset) is the only reliable injection point.  Returns an empty
    list for host-mode builds where no container is involved.

    Creates the host-side ccache directory when absent so the Docker
    bind-mount never targets a missing path.
    """
    if cfg.host_mode:
        return []
    ccache_host = cfg.workspace / "ccache"
    ccache_host.mkdir(exist_ok=True)
    return ["--runtime-args", f"-v {ccache_host}:/work/ccache:rw"]


def regenerate_yaml(cfg: BuildConfig, log: RunLogger, *, bsp: BspModel) -> None:
    """Run the topology-only kas YAML generator, writing to ``cfg.default_kas_yaml``."""
    log.step_start("gen_kas", target=cfg.image)
    output = cfg.default_kas_yaml
    opts = KasGenOptions(
        manifest=cfg.manifest_path,
        bblayers=cfg.bblayers_conf if cfg.bblayers_conf.is_file() else None,
        machine=cfg.machine,
        distro=cfg.distro,
        target=cfg.image,
        output=output,
        workspace=cfg.workspace,
        template=bsp.kas_template,
        skip_manifest=(bsp.manifest_kind != "repo-xml"),
    )
    write_yaml(opts)
    log.step_ok("gen_kas", yaml=str(output))
    artifact = f"build/tmp/deploy/images/{cfg.machine}/{cfg.image}-{cfg.machine}.wic"
    sys.stdout.write(f"INFO     artifact: {artifact}\n")
    sys.stdout.flush()


def run_build(
    cfg: BuildConfig,
    log: RunLogger,
    *,
    kas_yaml: Path,
    overlay_source: Path,
) -> int:
    """Run `kas-container build <kas_yaml>:<overlay>` with the measurement harness.

    Returns the build exit code. Does not raise - caller decides how to
    react to a nonzero status.

    ``kas_yaml`` must live under ``cfg.bsp_root`` (the kas-container
    bind mount). ``overlay_source`` is the absolute path to the static
    overlay; this function symlinks it into ``<bsp_root>/.varis/overlays/``
    so it is reachable from inside the container.
    """
    log.step_start("kas_build", yaml=str(kas_yaml), overlay=str(overlay_source))
    cfg.measurements_dir.mkdir(parents=True, exist_ok=True)

    kas_yaml_rel = _resolve_user_yaml(cfg, kas_yaml)
    overlay_rel = materialize_overlay(cfg, overlay_source)

    # Shared state for the pump, du sampler, and heartbeat. Writes from
    # one thread only (pump: last_event_ts; sampler: prev_du_bytes /
    # cur_du_bytes) plus read-only access from heartbeat means the GIL
    # suffices - no lock needed for these single-slot updates.
    state: dict[str, float | int] = {
        "last_event_ts": time.monotonic(),
        "cur_du_bytes": 0,
        "prev_du_bytes": 0,
    }
    stop_event = threading.Event()
    du_path = log.du_samples_path

    def du_loop() -> None:
        while not stop_event.wait(timeout=30):
            build_tmp = cfg.bsp_root / "build" / "tmp"
            if not build_tmp.is_dir():
                continue
            try:
                size = subprocess.run(
                    ["du", "-sb", str(build_tmp)],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                if size.returncode == 0:
                    bytes_ = int(size.stdout.split()[0])
                    with du_path.open("a") as fh:
                        fh.write(f"{int(time.time())}\t{bytes_}\n")
                    state["prev_du_bytes"] = state["cur_du_bytes"]
                    state["cur_du_bytes"] = bytes_
            except Exception:  # noqa: BLE001 - sampler must not crash the build
                continue

    sampler = threading.Thread(target=du_loop, daemon=True)
    sampler.start()

    # Build command - prefer /usr/bin/time -v when available.
    cmd: list[str] = []
    if shutil.which("/usr/bin/time"):
        cmd = ["/usr/bin/time", "-v", "-o", str(log.time_log_path), "--"]
    cmd += ["kas-container", *_ccache_args(cfg), "build", f"{kas_yaml_rel}:{overlay_rel}"]

    log.info(f"exec: {' '.join(cmd)}")
    # The pump thread writes every line to kas.log for `varis log` to tail,
    # parses bitbake counters into a rich Progress bar, and surfaces
    # ERROR/WARNING/FATAL/QA Issue lines above the bar.  Nothing goes to
    # sys.stdout directly - the Progress instance owns the terminal.
    #
    # PTY plumbing: openpty() gives us a (master, slave) fd pair. We pass
    # slave as the child's stdout/stderr so kas-container's `[ -t 1 ]`
    # check sees a TTY and adds `-t -i` to `docker run`, which in turn
    # makes bitbake's knotty UI interactive. knotty uses CR (no newline)
    # to redraw its status line in place, so we read chunks and split on
    # \r, \n, or \r\n manually instead of line-iterating.
    rc: int | None = None
    terminated = False
    master_fd, slave_fd = pty.openpty()
    try:
        with log.kas_log_path.open("w", encoding="utf-8", buffering=1) as kas_log:
            proc = subprocess.Popen(
                cmd,
                cwd=cfg.bsp_root,
                # stdin must be a TTY too: kas-container sees stdout as a
                # TTY (via slave_fd) and passes -t -i to docker, which
                # then requires stdin to also be a TTY or it refuses with
                # "cannot attach stdin to a TTY-enabled container
                # because stdin is not a terminal". Sharing the same pty
                # slave across stdin/stdout/stderr satisfies that check.
                # We never write to master_fd, so the child's stdin reads
                # block indefinitely - which is fine for a batch build.
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                env=_build_env(cfg),
                preexec_fn=os.setsid,
                close_fds=True,
            )
            os.close(slave_fd)
            slave_fd = -1

            progress = Progress(
                TextColumn("[cyan]kas_build[/]"),
                BarColumn(),
                TextColumn("{task.completed}/{task.total} tasks"),
                TextColumn("{task.fields[recipe]}"),
                TextColumn("[dim]live {task.fields[stall]}  {task.fields[du]}[/]"),
                TimeElapsedColumn(),
            )

            with progress:
                task_id = progress.add_task(
                    "kas_build",
                    total=None,
                    recipe="",
                    stall="0s",
                    du="-",
                    expansions=0,
                )

                def _process_line(line: str) -> None:
                    nonlocal last_total, last_completed, expansion_count, current_recipe
                    kas_log.write(line + "\n")
                    kas_log.flush()
                    state["last_event_ts"] = time.monotonic()

                    m = CURRENT_RUNNING.search(line)
                    if m:
                        completed, total = int(m.group(1)), int(m.group(2))
                        if last_total and abs(total - last_total) / max(last_total, 1) >= 0.05:
                            expansion_count += 1
                            verb = "expanded" if total > last_total else "reduced"
                            progress.console.print(f"[yellow]task graph {verb}: {last_total} -> {total}[/]")
                        progress.update(
                            task_id,
                            completed=completed,
                            total=total,
                            recipe=current_recipe,
                            expansions=expansion_count,
                        )
                        last_total, last_completed = total, completed
                        return

                    m = RUNNING_TASK.search(line)
                    if m:
                        completed, total = int(m.group(1)), int(m.group(2))
                        current_recipe = m.group(3).rsplit("/", 1)[-1]
                        if last_total and abs(total - last_total) / max(last_total, 1) >= 0.05:
                            expansion_count += 1
                            verb = "expanded" if total > last_total else "reduced"
                            progress.console.print(f"[yellow]task graph {verb}: {last_total} -> {total}[/]")
                        progress.update(
                            task_id,
                            completed=completed,
                            total=total,
                            recipe=current_recipe,
                            expansions=expansion_count,
                        )
                        last_total, last_completed = total, completed
                        return

                    m = PARSE_PROGRESS.search(line)
                    if m:
                        done, total = int(m.group(2)), int(m.group(3))
                        progress.update(task_id, completed=done, total=total, recipe="parsing recipes")
                        return

                    m = SETSCENE_RUNNING.search(line)
                    if m:
                        completed, total = int(m.group(1)), int(m.group(2))
                        progress.update(task_id, completed=completed, total=total, recipe="setscene")
                        return

                    if SEVERITY_PASSTHROUGH.search(line):
                        progress.console.print(line)

                last_total = 0
                last_completed = 0  # noqa: F841 - kept for future delta / debugging
                expansion_count = 0
                current_recipe = ""

                def _pump() -> None:
                    buf = b""
                    while True:
                        try:
                            chunk = os.read(master_fd, 8192)
                        except OSError:
                            # EIO fires on Linux when the slave side closes
                            # (child exited). Treat as EOF.
                            break
                        if not chunk:
                            break
                        buf += chunk
                        while True:
                            m = LINE_SPLIT_RE.search(buf)
                            if m is None:
                                break
                            raw = buf[: m.start()]
                            buf = buf[m.end() :]
                            if not raw:
                                continue
                            line = _strip_ansi(raw.decode("utf-8", errors="replace"))
                            _process_line(line)
                    if buf:
                        tail = _strip_ansi(buf.decode("utf-8", errors="replace"))
                        if tail:
                            _process_line(tail)

                def _heartbeat() -> None:
                    # Tick once per second so the user can see whether the
                    # build is genuinely wedged ("5m30s live") or just in a
                    # quiet compile phase ("12s live, +210M"). du-delta is
                    # the difference between the two most recent 30s
                    # samples; it lags but confirms actual disk work.
                    while not stop_event.wait(timeout=1):
                        if proc.poll() is not None:
                            break
                        stall = int(time.monotonic() - state["last_event_ts"])
                        delta = state["cur_du_bytes"] - state["prev_du_bytes"]
                        progress.update(
                            task_id,
                            stall=_fmt_stall(stall),
                            du=_fmt_du(delta),
                        )

                pump = threading.Thread(target=_pump, daemon=True)
                pump.start()
                heartbeat = threading.Thread(target=_heartbeat, daemon=True)
                heartbeat.start()
                try:
                    rc = proc.wait()
                except KeyboardInterrupt:
                    os.killpg(proc.pid, signal.SIGINT)
                    rc = proc.wait()
                stop_event.set()
                pump.join(timeout=5)
                heartbeat.join(timeout=2)

        if rc == 0:
            deploy = cfg.bsp_root / "build" / "tmp" / "deploy" / "images" / cfg.machine
            log.step_ok("kas_build", deploy_dir=str(deploy), exit_code=rc)
        else:
            log.step_fail(
                "kas_build",
                reason=f"exit_code={rc}",
                exit_code=rc,
                kas_log=str(log.kas_log_path),
            )
        terminated = True
    finally:
        if not terminated:
            # Wrapper crashed before the normal step_ok/step_fail path.  Emit
            # a terminal event anyway so events.jsonl never dead-ends at
            # step_start and `varis triage` has something to find.
            if rc == 0:
                deploy = cfg.bsp_root / "build" / "tmp" / "deploy" / "images" / cfg.machine
                log.step_ok("kas_build", deploy_dir=str(deploy), exit_code=rc)
            else:
                log.step_fail(
                    "kas_build",
                    reason=f"exit_code={rc}" if rc is not None else "wrapper-crash",
                    exit_code=rc if rc is not None else -1,
                    kas_log=str(log.kas_log_path),
                )
        stop_event.set()
        sampler.join(timeout=5)
        if slave_fd != -1:
            try:
                os.close(slave_fd)
            except OSError:
                pass
        try:
            os.close(master_fd)
        except OSError:
            pass
    return rc if rc is not None else -1


def _build_env(cfg: BuildConfig, python_executable: Path | None = None) -> dict[str, str]:
    """Return the environment to hand to kas-container.

    Keeps SSTATE_DIR, DL_DIR, NPROC, and KAS_* from the caller's shell
    (these are the knobs kas-container actually reads) plus a stable
    PATH and HOME so the subprocess behaves the same as an interactive
    shell run.

    KAS_WORK_DIR is forced to the BSP-specific subtree
    (``cfg.bsp_root`` = ``workspace/<bsp_family>``) so kas-container
    bind-mounts that subtree as ``/work`` inside the container. With
    this setting, in-container paths (``/work/sources/...``,
    ``/work/forks/...``, ``/work/build/...``, ``/work/ccache``) are
    byte-identical between NXP and TI, so neither the kas template nor
    any recipe needs to know which BSP it is in.

    The ccache bind-mount (``/work/ccache``) is injected at the call
    site via ``_ccache_args()`` as a ``--runtime-args`` CLI flag, not
    here.  ``kas-container`` unconditionally overwrites ``KAS_RUNTIME_ARGS``
    before its option-parsing loop, making env-var injection unreliable.

    ``python_executable`` overrides the host-mode BB_PYTHON3 and PATH
    interpreter. Used by VARIS-19 stress-parse to point bitbake at a
    locally-built CPython (e.g. one with the obmalloc atfork patch)
    without reinstalling varis under it. When None, host mode defaults
    to ``sys.executable``.
    """
    passthrough = {
        k: v
        for k, v in os.environ.items()
        if k.startswith(("KAS_", "BB_", "SSTATE_", "DL_", "NPROC", "PATH", "HOME", "USER"))
    }
    # PATH might not have leaked via the startswith rule if the shell
    # exported it without prefix; ensure it is present.
    passthrough.setdefault("PATH", os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"))
    passthrough.setdefault("HOME", os.environ.get("HOME", "/tmp"))
    passthrough["KAS_WORK_DIR"] = str(cfg.bsp_root)

    # In host mode, prepend the varis interpreter's bin dir to PATH and
    # set BB_PYTHON3 so bitbake's bin/bitbake re-execs into the same
    # Python varis was installed under (a uv tool venv pinned to 3.12
    # via `uv tool install --python 3.12`). The bitbake-server and
    # bitbake-worker subprocesses inherit the interpreter through
    # sys.executable in bb.server.process._startServer / bb.runqueue,
    # so dispatching the entry-point script is sufficient.
    if cfg.host_mode:
        if python_executable is not None:
            py_path = python_executable.resolve()
            py_bin = str(py_path.parent)
            passthrough["BB_PYTHON3"] = str(py_path)
        else:
            py_bin = sysconfig.get_path("scripts")
            passthrough["BB_PYTHON3"] = sys.executable
        passthrough["PATH"] = py_bin + os.pathsep + passthrough.get("PATH", "")
    return passthrough


def run_shell(
    cfg: BuildConfig,
    log: RunLogger,
    args: list[str],
    command: str | None = None,
    *,
    kas_yaml: Path,
    overlay_source: Path,
) -> int:
    """Drop into a kas-container shell, passing through extra args.

    When ``command`` is provided, kas-container runs it non-interactively
    via ``-c <command>`` instead of opening an interactive shell. The
    overlay is layered in via the same colon-joined arg as ``run_build``.

    When ``cfg.host_mode`` is True, plain ``kas shell`` runs directly on
    the host (no kas-container wrapper, no Docker). The host must have
    the bitbake build prereqs installed (zstd, git, ...) and a
    bitbake-supported Python on PATH.
    """
    log.step_start("kas_shell", command=command, host_mode=cfg.host_mode)
    kas_yaml_rel = _resolve_user_yaml(cfg, kas_yaml)
    overlay_rel = materialize_overlay(cfg, overlay_source)
    exe = "kas" if cfg.host_mode else "kas-container"
    cmd = [exe, *_ccache_args(cfg), "shell", f"{kas_yaml_rel}:{overlay_rel}"]
    if command is not None:
        cmd.extend(["-c", command])
    cmd.extend(args)
    proc = subprocess.Popen(cmd, cwd=cfg.bsp_root, env=_build_env(cfg))
    rc = proc.wait()
    log.step_ok("kas_shell", exit_code=rc)
    return rc


def run_shell_capture(
    cfg: BuildConfig,
    log: RunLogger,
    command: str,
    stdout_path: Path,
    *,
    kas_yaml: Path,
    overlay_source: Path,
    step: str = "kas_shell_capture",
    python_executable: Path | None = None,
) -> int:
    """Run ``kas-container shell -c <command>`` with output captured to file.

    Sister to :func:`run_shell`. Same env+cwd plumbing via
    :func:`_build_env`; the only difference is that stdout and stderr
    are merged and redirected to ``stdout_path`` instead of inheriting
    the parent terminal. Returns the kas-container exit code.

    Used by :mod:`bspctl.steps.stress_parse` to capture each
    ``bitbake -p`` iteration's output to its own log file for offline
    fork-race signature scanning.

    ``python_executable`` is forwarded to :func:`_build_env` so the
    kas shell's PATH and BB_PYTHON3 point at a caller-chosen interpreter
    (VARIS-19 obmalloc-patch validation).
    """
    log.step_start(step, command=command, stdout_path=str(stdout_path), host_mode=cfg.host_mode)
    kas_yaml_rel = _resolve_user_yaml(cfg, kas_yaml)
    overlay_rel = materialize_overlay(cfg, overlay_source)
    exe = "kas" if cfg.host_mode else "kas-container"
    cmd = [exe, *_ccache_args(cfg), "shell", f"{kas_yaml_rel}:{overlay_rel}", "-c", command]
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    with stdout_path.open("wb") as fh:
        proc = subprocess.Popen(
            cmd,
            cwd=cfg.bsp_root,
            env=_build_env(cfg, python_executable=python_executable),
            stdout=fh,
            stderr=subprocess.STDOUT,
        )
        rc = proc.wait()
    log.step_ok(step, exit_code=rc)
    return rc
