"""Source `setup-environment` in a clean bash subshell for the QLI build.

The Qualcomm QLI tree ships a ``setup-environment`` script (a repo-sync
linkfile) that writes ``build/conf/{local,bblayers}.conf`` on disk. As with
the NXP ``var-setup-release.sh`` step, only those files need to survive; the
env vars exported inside the subshell do not, so we discard them.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

from bakar.diagnostics import detect_buildtools

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
    # PC2 (and any rolling-distro host) ships a gcc too new for scarthgap, so
    # source Yocto's buildtools-extended env-setup first - it puts the pinned
    # gcc 13.4 ahead of the system one on PATH. release_key=None on purpose:
    # qcom's oe-core is at <ws>/qcom/layers/poky, not <ws>/openembedded-core, so
    # resolve_oe_core_release_key can't fit it; the flat buildtools_dir /
    # BAKAR_BUILDTOOLS_DIR resolution (detect_buildtools's release_key=None path,
    # the same one BYO uses for non-oe-core families) is the correct one here.
    toolchain = detect_buildtools(release_key=None)
    if toolchain.present and toolchain.env_script is not None:
        command = f". {toolchain.env_script} && . {script}"
    else:
        if not toolchain.present:
            log.info(f"setup_env: no buildtools-extended toolchain sourced ({toolchain.detail})")
        command = f". {script}"
    subprocess.run(  # pragma: no cover
        ["bash", "-c", command],
        cwd=qcom,
        env=env,
        check=True,
    )
    if not cfg.bblayers_conf.is_file():
        raise RuntimeError(f"{cfg.bblayers_conf} missing after setup-environment; check the script output above.")
    log.step_ok("setup_env", bblayers=str(cfg.bblayers_conf))
