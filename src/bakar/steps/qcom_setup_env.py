"""Source `setup-environment` in a clean bash subshell for the QLI build.

The Qualcomm QLI tree ships a ``setup-environment`` script (a repo-sync
linkfile) that writes ``build/conf/{local,bblayers}.conf`` on disk. As with
the NXP ``var-setup-release.sh`` step, only those files need to survive; the
env vars exported inside the subshell do not, so we discard them.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

from bakar.steps.qcom_common import qcom_buildtools_prefix, qcom_env

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
    env = qcom_env(cfg)
    command = f"{qcom_buildtools_prefix(log, step='setup_env')}. {script}"
    subprocess.run(  # pragma: no cover
        ["bash", "-c", command],
        cwd=qcom,
        env=env,
        check=True,
    )
    if not cfg.bblayers_conf.is_file():
        raise RuntimeError(f"{cfg.bblayers_conf} missing after setup-environment; check the script output above.")
    log.step_ok("setup_env", bblayers=str(cfg.bblayers_conf))
