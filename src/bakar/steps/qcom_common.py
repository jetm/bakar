"""Shared environment helpers for the Qualcomm QLI setup-env and build steps.

Both the ``setup-environment`` sourcing step and the ``bitbake`` build step run
in a controlled bash subshell rooted at ``<workspace>/qcom`` with the same
QLI-specific environment (MACHINE/DISTRO/QCOM_SELECTED_BSP/EXTRALAYERS) and the
same pinned buildtools-extended toolchain sourced first on PATH. This module is
the single source of truth for both so the two steps cannot drift.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from bakar.diagnostics import detect_buildtools

if TYPE_CHECKING:
    from bakar.config import BuildConfig
    from bakar.observability import RunLogger


def qcom_env(cfg: BuildConfig) -> dict[str, str]:
    """The QLI subshell environment shared by setup-env and build.

    Inherit the caller's environment and override only the QLI-specific knobs.
    A full ``env -i`` was tempting for reproducibility, but bitbake aborts its
    sanity check without a UTF-8 locale (and needs the host's network/SSL vars
    for any cache miss), so a stripped env fails the build - the proven manual
    flow ran under the inherited login environment. The buildtools env-setup
    script (sourced first in the same shell) puts the pinned gcc ahead of the
    inherited PATH, so inheriting PATH is safe. EXTRALAYERS is hardcoded to the
    QLI product-SDK layer set; there is no BuildConfig field for it in this pass.
    """
    env = dict(os.environ)
    env.update(
        {
            "MACHINE": cfg.machine,
            "DISTRO": cfg.distro,
            "QCOM_SELECTED_BSP": "custom",
            "EXTRALAYERS": "meta-qcom-qim-product-sdk meta-innodisk-iq",
        }
    )
    # bitbake requires a UTF-8 locale; fall back to the always-available
    # C.UTF-8 only when the inherited locale is not already UTF-8.
    if "utf" not in (env.get("LC_ALL", "") + env.get("LANG", "")).lower():
        env["LC_ALL"] = "C.UTF-8"
    return env


def qcom_buildtools_prefix(log: RunLogger | None = None, *, step: str = "setup_env") -> str:
    """Return a bash source-prefix (``. <env-script> && ``) for the pinned
    buildtools-extended toolchain, or ``""`` when none needs sourcing.

    PC2 (and any rolling-distro host) ships a gcc too new for scarthgap, so the
    buildtools-extended env-setup puts the pinned gcc 13.4 ahead of the system
    one on PATH. ``release_key=None`` on purpose: qcom's oe-core is at
    ``<ws>/qcom/layers/poky``, not ``<ws>/openembedded-core``, so
    ``resolve_oe_core_release_key`` can't fit it; the flat buildtools_dir /
    ``BAKAR_BUILDTOOLS_DIR`` resolution (the same one BYO uses for non-oe-core
    families) is the correct one here.
    """
    toolchain = detect_buildtools(release_key=None)
    if toolchain.present and toolchain.env_script is not None:
        return f". {toolchain.env_script} && "
    if not toolchain.present and log is not None:
        log.info(f"{step}: no buildtools-extended toolchain sourced ({toolchain.detail})")
    return ""
