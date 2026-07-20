"""Run the QLI ``bitbake`` build in a bash subshell.

Unlike the kas families, a QLI build is not a kas build: it sources the QLI
``setup-environment`` to enter BUILDDIR (``build-<distro>``), then runs
``bitbake`` directly. ``bitbake`` must run in the SAME shell that sourced
buildtools + ``setup-environment`` (the exported env does not survive across
processes), so the whole pipeline is one ``bash -c`` invocation.

NOTE: this path has no kas live-UI / stall-watchdog (those are kas-specific);
v1 streams ``bitbake`` output line-by-line to the operator and the run log.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

from bakar.steps.qcom_common import qcom_buildtools_prefix, qcom_env

if TYPE_CHECKING:
    from pathlib import Path

    from bakar.config import BuildConfig
    from bakar.observability import RunLogger


def _stream_build(command: str, *, cwd: Path, env: dict[str, str], log_path: Path) -> int:
    """Run ``bash -c command`` streaming stdout to the operator and ``log_path``.

    Non-PTY ``Popen`` mirroring ``remote_dispatch._stream_remote_build``:
    stdout is read line-by-line, echoed live, and mirrored into the run log.
    ``errors="replace"`` matches kas_build's decode convention so a non-UTF-8
    byte in Yocto output cannot crash the stream. Returns the bitbake exit code.
    """
    proc = subprocess.Popen(
        ["bash", "-c", command],
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.stdout is not None  # PIPE is set above
    with log_path.open("w", encoding="utf-8") as fh:
        for line in proc.stdout:
            print(line, end="")
            fh.write(line)
    return proc.wait()


def run(
    cfg: BuildConfig,
    log: RunLogger,
    *,
    target: str,
    keep_going: bool = False,
    dry_run: bool = False,
) -> int:
    """Source ``setup-environment`` and run ``bitbake <target>`` under it.

    Returns the bitbake exit code (non-zero is returned, not raised, so the
    caller's ``_finish_build`` renders the triage hint like the kas path).
    """
    log.step_start("qcom_build", target=target, machine=cfg.machine, distro=cfg.distro)
    if dry_run:
        # Mirror the kas dry-run contract: never invoke the builder.
        log.step_ok("qcom_build", dry_run=True, target=target)
        return 0

    qcom = cfg.workspace / cfg.workspace_subdir
    bitbake = "bitbake " + ("-k " if keep_going else "") + target
    command = f"{qcom_buildtools_prefix(log, step='qcom_build')}. ./setup-environment && {bitbake}"
    # Emit the artifacts bakar monitor/log/triage read: bitbake writes its
    # base64-pickled event log where _build_progress reads it, and the stream
    # lands in kas.log (bakar's conventional build-log name that
    # _recent_kas_errors, `bakar log`, and `bakar triage` all read). qcom is a
    # host/no-container build, so bitbake writes the host path directly.
    env = qcom_env(cfg)
    env["BB_DEFAULT_EVENTLOG"] = str(log.run_dir / "bitbake_eventlog.json")
    rc = _stream_build(command, cwd=qcom, env=env, log_path=log.run_dir / "kas.log")
    if rc != 0:
        log.step_fail("qcom_build", reason=f"bitbake exited {rc}", target=target)
    else:
        log.step_ok("qcom_build", target=target)
    return rc
