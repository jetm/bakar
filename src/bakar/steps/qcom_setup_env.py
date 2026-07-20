"""Source `setup-environment` in a clean bash subshell for the QLI build.

The Qualcomm QLI tree ships a ``setup-environment`` script (a repo-sync
linkfile) that writes ``build/conf/{local,bblayers}.conf`` on disk. As with
the NXP ``var-setup-release.sh`` step, only those files need to survive; the
env vars exported inside the subshell do not, so we discard them.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bakar.config import BuildConfig
    from bakar.observability import RunLogger


def run(cfg: BuildConfig, log: RunLogger) -> None:
    log.step_start("setup_env", machine=cfg.machine, distro=cfg.distro)
    qcom = cfg.workspace / cfg.workspace_subdir
    script = qcom / "setup-environment"
    if not script.exists():
        raise FileNotFoundError(
            f"{script} missing - did repo sync complete? "
            "The script is a repo-sync linkfile produced by the QLI manifest checkout."
        )
    # Use env -i to avoid fish/bash env leakage, then set only what the
    # script actually reads. EXTRALAYERS is hardcoded to the QLI product-SDK
    # layer set; there is no BuildConfig field for it in this pass.
    env = {
        "HOME": str(qcom),
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "MACHINE": cfg.machine,
        "DISTRO": cfg.distro,
        "QCOM_SELECTED_BSP": "custom",
        "EXTRALAYERS": "meta-qcom-qim-product-sdk meta-innodisk-iq",
    }
    subprocess.run(  # pragma: no cover
        ["bash", "-c", f". {script}"],
        cwd=qcom,
        env=env,
        check=True,
    )
    if not cfg.bblayers_conf.is_file():
        raise RuntimeError(f"{cfg.bblayers_conf} missing after setup-environment; check the script output above.")
    log.step_ok("setup_env", bblayers=str(cfg.bblayers_conf))
