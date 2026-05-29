"""QEMU run step for meta-avocado builds."""

from __future__ import annotations

import subprocess
import sys
import termios
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from bspctl.config import BuildConfig
    from bspctl.observability import RunLogger


def _find_meta_avocado_dir(kas_yaml: Path) -> Path:
    """Walk up from kas_yaml to the meta-avocado repository root."""
    for parent in kas_yaml.resolve().parents:
        if parent.name == "meta-avocado":
            return parent
    raise ValueError(f"{kas_yaml} is not inside a meta-avocado checkout")


def resolve_run_script(kas_yaml: Path, *, swtpm: bool) -> Path:
    """Return the path to the run script for the given machine YAML."""
    meta_avocado_dir = _find_meta_avocado_dir(kas_yaml)
    suffix = "-swtpm" if swtpm else ""
    return meta_avocado_dir / "meta-avocado-qemu" / "scripts" / f"run-{kas_yaml.stem}{suffix}"


def run_qemu(cfg: BuildConfig, log: RunLogger, *, swtpm: bool, kas_yaml: Path) -> int:
    """Invoke the avocado run script from cfg.bsp_root.

    stdin/stdout/stderr are inherited so QEMU runs interactively.
    The run scripts assume cwd is the bsp_root (e.g. sources/build-qemux86-64).
    """
    script = resolve_run_script(kas_yaml, swtpm=swtpm)
    if not script.exists():
        raise FileNotFoundError(f"Run script not found: {script}")

    build_dir = cfg.bsp_root / "build"
    if not build_dir.exists():
        raise RuntimeError(
            f"Build output not found at {build_dir}. "
            "Run `bspctl build` first, then provision the disk image with "
            "`bspctl build kas/machine/qemux86-64.yml:kas/target/qemu-provision.yml`."
        )

    log.step_start("run_qemu", script=str(script), swtpm=swtpm)
    saved_attrs = None
    if sys.stdin.isatty():
        saved_attrs = termios.tcgetattr(sys.stdin.fileno())
    try:
        proc = subprocess.Popen(["bash", str(script)], cwd=cfg.bsp_root)
        rc = proc.wait()
    finally:
        if saved_attrs is not None:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, saved_attrs)
    log.step_ok("run_qemu", exit_code=rc)
    return rc
