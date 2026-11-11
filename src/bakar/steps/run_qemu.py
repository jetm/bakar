"""QEMU run step for meta-avocado builds."""

from __future__ import annotations

import subprocess
import sys
import termios
from typing import TYPE_CHECKING

from bakar.bsp_detect import meta_avocado_worktree

if TYPE_CHECKING:
    from pathlib import Path

    from bakar.config import BuildConfig
    from bakar.observability import RunLogger


def _find_meta_avocado_dir(kas_yaml: Path) -> Path:
    """Walk up from kas_yaml to the meta-avocado repository root.

    A linked worktree's directory is not named ``meta-avocado``, so it is found
    through git's own layout first.
    """
    wt = meta_avocado_worktree(kas_yaml)
    if wt is not None:
        return wt.checkout
    for parent in kas_yaml.resolve().parents:
        if parent.name == "meta-avocado":
            return parent
    raise ValueError(f"{kas_yaml} is not inside a meta-avocado checkout")


def resolve_run_script(kas_yaml: Path) -> Path:
    """Return the path to meta-avocado's single QEMU runner.

    One ``run-qemu <machine> [--tpm]`` script serves every machine; the machine
    comes from the kas YAML's stem, not from the script's name.
    """
    meta_avocado_dir = _find_meta_avocado_dir(kas_yaml)
    return meta_avocado_dir / "meta-avocado-qemu" / "scripts" / "run-qemu"


# run-qemu rejects --tpm for every other machine ("--tpm is only supported for
# qemux86-64"), so the default-on --swtpm must not turn into an argument there.
_TPM_MACHINES = frozenset({"qemux86-64"})


def run_qemu(cfg: BuildConfig, log: RunLogger, *, swtpm: bool, kas_yaml: Path) -> int:
    """Invoke the avocado run script from cfg.bsp_root.

    stdin/stdout/stderr are inherited so QEMU runs interactively.
    The run scripts assume cwd is the bsp_root (e.g. sources/build-qemux86-64).
    """
    script = resolve_run_script(kas_yaml)
    machine = kas_yaml.stem
    argv = ["bash", str(script), machine]
    if swtpm and machine in _TPM_MACHINES:
        argv.append("--tpm")
    if not script.exists():
        raise FileNotFoundError(f"Run script not found: {script}")

    build_dir = cfg.bsp_root / "build"
    if not build_dir.exists():
        raise RuntimeError(
            f"Build output not found at {build_dir}. "
            "Run `bakar build` first, then provision the disk image with "
            "`bakar build kas/machine/qemux86-64.yml:kas/target/qemu-provision.yml`."
        )

    log.step_start("run_qemu", script=str(script), swtpm=swtpm)
    saved_attrs = None
    if sys.stdin.isatty():
        saved_attrs = termios.tcgetattr(sys.stdin.fileno())
    try:
        proc = subprocess.Popen(argv, cwd=cfg.bsp_root)
        rc = proc.wait()
    finally:
        if saved_attrs is not None:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, saved_attrs)
    log.step_ok("run_qemu", exit_code=rc)
    return rc
