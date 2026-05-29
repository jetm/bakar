"""Invoke `var-setup-release.sh` in a clean bash subshell.

The script is bash-only and writes ``build/conf/{local,bblayers}.conf`` on
disk. We only need those files to exist; the exported env vars inside the
subshell do not survive anyway, so we discard them.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bspctl.config import BuildConfig
    from bspctl.observability import RunLogger


def run(cfg: BuildConfig, log: RunLogger) -> None:
    log.step_start("setup_env", machine=cfg.machine, distro=cfg.distro)
    nxp = cfg.workspace / "nxp"
    script = nxp / "var-setup-release.sh"
    if not script.exists():
        raise FileNotFoundError(
            f"{script} missing - did repo sync complete? "
            "The script is a repo-sync linkfile that resolves to "
            "sources/meta-variscite-sdk-imx/scripts/var-setup-release.sh."
        )
    # Use env -i to avoid fish/bash env leakage, then set only what the
    # script actually reads.
    env = {
        "HOME": str(nxp),
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "MACHINE": cfg.machine,
        "DISTRO": cfg.distro,
        "EULA": "1",
    }
    subprocess.run(
        ["bash", "-c", f". {script} build"],
        cwd=nxp,
        env=env,
        check=True,
    )
    if not cfg.bblayers_conf.is_file():
        raise RuntimeError(f"{cfg.bblayers_conf} missing after var-setup-release.sh; check the script output above.")
    log.step_ok("setup_env", bblayers=str(cfg.bblayers_conf))
